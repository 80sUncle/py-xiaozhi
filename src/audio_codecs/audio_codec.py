import asyncio
import gc
import time
import ctypes
from collections import deque
from typing import Optional

import numpy as np
import opuslib
import sounddevice as sd
import soxr

from src.constants.constants import AudioConfig
from src.utils.config_manager import ConfigManager
from src.utils.logging_config import get_logger

try:
    from libs.webrtc_apm import WebRTCAudioProcessing, create_default_config
    WEBRTC_AVAILABLE = True
except ImportError:
    WEBRTC_AVAILABLE = False

logger = get_logger(__name__)


class AudioCodec:
    """
    音频编解码器，负责录音编码和播放解码
    主要功能：
    1. 录音：麦克风 -> WebRTC AEC处理 -> 重采样16kHz -> Opus编码 -> 发送
    2. 播放：接收 -> Opus解码24kHz -> 播放队列 -> 扬声器
    """

    def __init__(self):
        # 获取配置管理器
        self.config = ConfigManager.get_instance()

        # Opus编解码器：录音16kHz编码，播放24kHz解码
        self.opus_encoder = None
        self.opus_decoder = None

        # 设备信息
        self.device_input_sample_rate = None
        self.device_output_sample_rate = None
        self.mic_device_id = None  # 麦克风设备ID
        self.reference_device_id = None  # 参考信号设备ID（如BlackHole）

        # 重采样器：WebRTC AEC后重采样到16kHz，播放重采样到设备采样率
        self.aec_post_resampler = None  # 设备采样率(AEC后) -> 16kHz
        self.output_resampler = None  # 24kHz -> 设备采样率(播放用)

        # 重采样缓冲区
        self._resample_aec_post_buffer = deque()
        self._resample_output_buffer = deque()

        self._device_input_frame_size = None
        self._is_closing = False

        # 音频流对象
        self.input_stream = None  # 录音流
        self.output_stream = None  # 播放流

        # 队列：唤醒词检测和播放缓冲
        self._wakeword_buffer = asyncio.Queue(maxsize=100)
        self._output_buffer = asyncio.Queue(maxsize=500)

        # 实时编码回调（直接发送，不走队列）
        self._encoded_audio_callback = None

        # WebRTC AEC组件 - 照搬quick_realtime_test.py
        self.webrtc_apm = None
        self.webrtc_capture_config = None
        self.webrtc_render_config = None
        self.webrtc_enabled = False
        self._device_frame_size = None  # 设备采样率的10ms帧大小
        
        # 参考信号缓冲区（从参考设备直接读取，如BlackHole）
        self._reference_buffer = deque()
        self.reference_stream = None  # 参考信号输入流
        self.reference_device_sample_rate = None  # 参考设备采样率
        self._reference_frame_size = None  # 参考设备10ms帧大小
        self.reference_resampler = None  # 24kHz -> 设备采样率重采样器


    async def initialize(self):
        """
        初始化音频设备.
        """
        try:
            # 显示并选择音频设备 - 照搬quick_realtime_test.py
            await self._select_audio_devices()
            
            input_device_info = sd.query_devices(self.mic_device_id or sd.default.device[0])
            output_device_info = sd.query_devices(sd.default.device[1])
            self.device_input_sample_rate = int(input_device_info["default_samplerate"])
            self.device_output_sample_rate = int(
                output_device_info["default_samplerate"]
            )
            frame_duration_sec = AudioConfig.FRAME_DURATION / 1000
            self._device_input_frame_size = int(
                self.device_input_sample_rate * frame_duration_sec
            )

            # 获取参考设备信息
            if self.reference_device_id is not None:
                ref_device_info = sd.query_devices(self.reference_device_id)
                self.reference_device_sample_rate = int(ref_device_info["default_samplerate"])
                self._reference_frame_size = int(
                    self.reference_device_sample_rate * frame_duration_sec
                )
                logger.info(f"参考设备: {ref_device_info['name']} - {self.reference_device_sample_rate}Hz")

            logger.info(
                f"输入采样率: {self.device_input_sample_rate}Hz, 输出: {self.device_output_sample_rate}Hz"
            )
            await self._create_resamplers()
            sd.default.samplerate = None
            sd.default.channels = AudioConfig.CHANNELS
            sd.default.dtype = np.int16
            await self._create_streams()
            self.opus_encoder = opuslib.Encoder(
                AudioConfig.INPUT_SAMPLE_RATE,
                AudioConfig.CHANNELS,
                opuslib.APPLICATION_AUDIO,
            )
            self.opus_decoder = opuslib.Decoder(
                AudioConfig.OUTPUT_SAMPLE_RATE, AudioConfig.CHANNELS
            )
            
            # 初始化WebRTC AEC - 照搬quick_realtime_test.py
            await self._initialize_webrtc_aec()
            
            logger.info("音频初始化完成")
        except Exception as e:
            logger.error(f"初始化音频设备失败: {e}")
            await self.close()
            raise

    async def _create_resamplers(self):
        """
        创建重采样器
        输入：移除原来的输入重采样器（设备采样率 -> 16kHz），改为AEC后重采样
        输出：24kHz -> 设备采样率（播放用）
        参考：24kHz -> 设备采样率（AEC参考用）
        """
        # AEC后重采样器：设备采样率 -> 16kHz（用于编码）
        if self.device_input_sample_rate != AudioConfig.INPUT_SAMPLE_RATE:
            self.aec_post_resampler = soxr.ResampleStream(
                self.device_input_sample_rate,
                AudioConfig.INPUT_SAMPLE_RATE,
                AudioConfig.CHANNELS,
                dtype="int16",
                quality="QQ",
            )
            logger.info(f"AEC后重采样: {self.device_input_sample_rate}Hz -> 16kHz")

        # 输出重采样器：24kHz -> 设备采样率
        if self.device_output_sample_rate != AudioConfig.OUTPUT_SAMPLE_RATE:
            self.output_resampler = soxr.ResampleStream(
                AudioConfig.OUTPUT_SAMPLE_RATE,
                self.device_output_sample_rate,
                AudioConfig.CHANNELS,
                dtype="int16",
                quality="QQ",
            )
            logger.info(
                f"输出重采样: {AudioConfig.OUTPUT_SAMPLE_RATE}Hz -> {self.device_output_sample_rate}Hz"
            )

        # 创建AEC参考信号重采样器：仅在没有硬件参考设备时使用24kHz播放音频
        if self.reference_device_id is None and AudioConfig.OUTPUT_SAMPLE_RATE != self.device_input_sample_rate:
            self.reference_resampler = soxr.ResampleStream(
                AudioConfig.OUTPUT_SAMPLE_RATE,
                self.device_input_sample_rate,
                AudioConfig.CHANNELS,
                dtype="int16",
                quality="QQ",
            )
            logger.info(
                f"AEC参考重采样(播放音频): {AudioConfig.OUTPUT_SAMPLE_RATE}Hz -> {self.device_input_sample_rate}Hz"
            )

    async def _initialize_webrtc_aec(self):
        """
        初始化WebRTC回声消除器 - 完全照搬quick_realtime_test.py的配置
        """
        if not WEBRTC_AVAILABLE:
            logger.warning("WebRTC AEC不可用，跳过初始化")
            return

        try:
            # 创建WebRTC APM实例
            self.webrtc_apm = WebRTCAudioProcessing()
            
            # 创建配置 - 完全照搬quick_realtime_test.py
            apm_config = create_default_config()
            
            # 平衡配置减少电音
            apm_config.echo.enabled = True
            apm_config.echo.mobile_mode = False  # AEC3
            apm_config.noise_suppress.enabled = True
            apm_config.noise_suppress.noise_level = 1  # HIGH (降低)
            apm_config.high_pass.enabled = False  # 关闭高通可能减少电音
            apm_config.gain_control2.enabled = False  # 关闭AGC2可能减少电音
            
            # 应用配置
            result = self.webrtc_apm.apply_config(apm_config)
            if result != 0:
                logger.error(f"WebRTC配置失败: {result}")
                return
            
            # 创建流配置（使用设备采样率，就像quick_realtime_test.py）
            # 如果有参考设备，使用参考设备的采样率，否则使用麦克风采样率
            render_sample_rate = self.reference_device_sample_rate or self.device_input_sample_rate
            
            self.webrtc_capture_config = self.webrtc_apm.create_stream_config(
                self.device_input_sample_rate, AudioConfig.CHANNELS
            )
            self.webrtc_render_config = self.webrtc_apm.create_stream_config(
                render_sample_rate, AudioConfig.CHANNELS
            )
            
            # 设置延迟为0以减少处理延迟 - 照搬quick_realtime_test.py
            self.webrtc_apm.set_stream_delay_ms(0)
            
            # 计算设备采样率的帧大小（10ms） - 照搬quick_realtime_test.py
            self._device_frame_size = int(self.device_input_sample_rate * 0.01)
            
            self.webrtc_enabled = True
            logger.info(f"WebRTC AEC3已启用 - {self.device_input_sample_rate}Hz, {self._device_frame_size}样本/帧")
            
        except Exception as e:
            logger.warning(f"WebRTC AEC初始化失败: {e}")
            self.webrtc_enabled = False

    async def _select_audio_devices(self):
        """
        显示并选择音频设备 - 照搬quick_realtime_test.py的逻辑
        """
        try:
            # 显示设备列表
            devices = sd.query_devices()
            logger.info("📋 可用音频设备:")
            for i, device in enumerate(devices):
                if device['max_input_channels'] > 0:
                    logger.info(f"  [{i}] {device['name']} - 输入{device['max_input_channels']}ch")

            # 自动检测常用设备
            blackhole_id = None
            mac_mic_id = None
            
            for i, device in enumerate(devices):
                device_name = device['name'].lower()
                if 'blackhole' in device_name and device['max_input_channels'] >= 2:
                    blackhole_id = i
                elif ('macbook' in device_name or 'built-in' in device_name) and 'microphone' in device_name:
                    mac_mic_id = i

            # 优先使用检测到的设备
            if mac_mic_id is not None:
                self.mic_device_id = mac_mic_id
                logger.info(f"🎤 检测到麦克风设备: [{mac_mic_id}] {devices[mac_mic_id]['name']}")
            else:
                # 使用默认设备
                self.mic_device_id = sd.default.device[0]
                logger.info(f"🎤 使用默认麦克风设备: [{self.mic_device_id}] {devices[self.mic_device_id]['name']}")

            if blackhole_id is not None:
                self.reference_device_id = blackhole_id
                logger.info(f"🔊 检测到参考设备: [{blackhole_id}] {devices[blackhole_id]['name']}")
                logger.info("✅ WebRTC AEC将使用BlackHole作为参考信号")
            else:
                self.reference_device_id = None
                logger.warning("⚠️ 未检测到BlackHole设备，AEC将使用播放音频作为参考信号")
                logger.info("💡 建议安装BlackHole虚拟音频设备以获得最佳AEC效果")

        except Exception as e:
            logger.warning(f"设备选择失败: {e}，使用默认设备")
            self.mic_device_id = None
            self.reference_device_id = None


    async def _create_streams(self):
        """
        创建音频流.
        """
        try:
            # 麦克风输入流 - 照搬quick_realtime_test.py，使用指定设备
            self.input_stream = sd.InputStream(
                device=self.mic_device_id,  # 指定麦克风设备ID
                samplerate=self.device_input_sample_rate,
                channels=AudioConfig.CHANNELS,
                dtype=np.int16,
                blocksize=self._device_input_frame_size,
                callback=self._input_callback,
                finished_callback=self._input_finished_callback,
                latency="low",
            )

            # 参考信号流 - 照搬quick_realtime_test.py，从BlackHole等设备读取
            if self.reference_device_id is not None:
                # 参考设备通常是立体声（如BlackHole 2ch）
                ref_channels = 2 if 'blackhole' in sd.query_devices(self.reference_device_id)['name'].lower() else AudioConfig.CHANNELS
                
                self.reference_stream = sd.InputStream(
                    device=self.reference_device_id,  # 指定参考设备ID
                    samplerate=self.reference_device_sample_rate,
                    channels=ref_channels,
                    dtype=np.int16,
                    blocksize=self._reference_frame_size,
                    callback=self._reference_callback,
                    finished_callback=self._reference_finished_callback,
                    latency="low",
                )

            # 根据设备支持的采样率选择输出采样率
            if self.device_output_sample_rate == AudioConfig.OUTPUT_SAMPLE_RATE:
                # 设备支持24kHz，直接使用
                output_sample_rate = AudioConfig.OUTPUT_SAMPLE_RATE
                device_output_frame_size = AudioConfig.OUTPUT_FRAME_SIZE
            else:
                # 设备不支持24kHz，使用设备默认采样率并启用重采样
                output_sample_rate = self.device_output_sample_rate
                device_output_frame_size = int(
                    self.device_output_sample_rate * (AudioConfig.FRAME_DURATION / 1000)
                )

            self.output_stream = sd.OutputStream(
                samplerate=output_sample_rate,
                channels=AudioConfig.CHANNELS,
                dtype=np.int16,
                blocksize=device_output_frame_size,
                callback=self._output_callback,
                finished_callback=self._output_finished_callback,
                latency="low",
            )

            self.input_stream.start()
            self.output_stream.start()
            
            # 启动参考信号流
            if self.reference_stream is not None:
                self.reference_stream.start()
                logger.info("参考信号流已启动")

        except Exception as e:
            logger.error(f"创建音频流失败: {e}")
            raise

    def _input_callback(self, indata, frames, time_info, status):
        """
        录音回调，硬件驱动调用
        处理流程：原始音频 -> WebRTC AEC -> 重采样16kHz -> 编码发送 + 唤醒词检测
        """
        if status and "overflow" not in str(status).lower():
            logger.warning(f"输入流状态: {status}")

        if self._is_closing:
            return

        try:
            audio_data = indata.copy().flatten()

            # WebRTC AEC处理 - 照搬quick_realtime_test.py的处理逻辑
            if self.webrtc_enabled and len(audio_data) == self._device_frame_size:
                audio_data = self._process_webrtc_aec(audio_data)

            # AEC后重采样到16kHz（如果设备不是16kHz）
            if self.aec_post_resampler is not None:
                audio_data = self._process_aec_post_resampling(audio_data)
                if audio_data is None:
                    return

            # 实时编码并发送（不走队列，减少延迟）
            if (
                self._encoded_audio_callback
                and len(audio_data) == AudioConfig.INPUT_FRAME_SIZE
            ):
                try:
                    pcm_data = audio_data.astype(np.int16).tobytes()
                    encoded_data = self.opus_encoder.encode(
                        pcm_data, AudioConfig.INPUT_FRAME_SIZE
                    )

                    if encoded_data:
                        self._encoded_audio_callback(encoded_data)

                except Exception as e:
                    logger.warning(f"实时录音编码失败: {e}")

            # 同时提供给唤醒词检测（走队列）
            self._put_audio_data_safe(self._wakeword_buffer, audio_data.copy())

        except Exception as e:
            logger.error(f"输入回调错误: {e}")

    def _process_webrtc_aec(self, audio_data):
        """
        WebRTC AEC处理录音信号 - 完全照搬quick_realtime_test.py第153-172行的逻辑
        """
        try:
            # 获取参考信号（设备采样率）
            reference_data = self._get_reference_signal()
            if reference_data is None:
                # 无参考信号时，使用静音作为参考
                reference_data = np.zeros(self._device_frame_size, dtype=np.int16)
            
            # 检查数据长度 - 照搬quick_realtime_test.py第154行
            if len(reference_data) == self._device_frame_size and len(audio_data.flatten()) == self._device_frame_size:
                # 准备ctypes缓冲区 - 照搬quick_realtime_test.py第155-159行
                capture_buffer = (ctypes.c_short * self._device_frame_size)(*audio_data.flatten())
                reference_buffer = (ctypes.c_short * self._device_frame_size)(*reference_data)
                processed_capture = (ctypes.c_short * self._device_frame_size)()
                processed_reference = (ctypes.c_short * self._device_frame_size)()

                # 处理参考流和捕获流 - 照搬quick_realtime_test.py第161-167行
                result1 = self.webrtc_apm.process_reverse_stream(
                    reference_buffer, self.webrtc_render_config, self.webrtc_render_config, processed_reference
                )
                result2 = self.webrtc_apm.process_stream(
                    capture_buffer, self.webrtc_capture_config, self.webrtc_capture_config, processed_capture
                )

                # 检查处理结果 - 照搬quick_realtime_test.py第169-172行
                if result1 == 0 and result2 == 0:
                    processed_audio = np.array(processed_capture, dtype=np.int16)
                    return processed_audio
                else:
                    logger.warning(f"WebRTC AEC处理失败: reverse={result1}, capture={result2}")
                    return audio_data
            else:
                logger.warning(f"WebRTC AEC数据长度不匹配: ref={len(reference_data)}, mic={len(audio_data)}")
                return audio_data

        except Exception as e:
            logger.warning(f"WebRTC AEC处理异常: {e}")
            return audio_data

    def _get_reference_signal(self):
        """
        获取AEC参考信号（设备采样率）
        """
        try:
            if len(self._reference_buffer) >= self._device_frame_size:
                # 从参考缓冲区取出一帧设备采样率数据
                frame_data = []
                for _ in range(self._device_frame_size):
                    frame_data.append(self._reference_buffer.popleft())
                return np.array(frame_data, dtype=np.int16)
            else:
                return None
        except Exception as e:
            logger.warning(f"获取参考信号失败: {e}")
            return None

    def _add_reference_signal(self, audio_data):
        """
        添加AEC参考信号（24kHz播放音频 -> 设备采样率参考信号）
        仅在没有硬件参考信号设备时使用
        """
        try:
            if not self.webrtc_enabled:
                return
            
            # 如果有硬件参考信号设备（如BlackHole），优先使用硬件信号
            if self.reference_device_id is not None:
                return  # 不使用播放音频作为参考信号

            # 没有硬件参考设备时，使用播放音频作为参考信号
            if self.reference_resampler is not None:
                resampled_data = self.reference_resampler.resample_chunk(audio_data, last=False)
                if len(resampled_data) > 0:
                    self._reference_buffer.extend(resampled_data.astype(np.int16))
            else:
                # 采样率相同时直接使用
                self._reference_buffer.extend(audio_data)

            # 限制缓冲区大小（避免延迟过大）
            max_buffer_size = self._device_frame_size * 10  # 最多缓存10帧
            while len(self._reference_buffer) > max_buffer_size:
                self._reference_buffer.popleft()

        except Exception as e:
            logger.warning(f"添加参考信号失败: {e}")

    def _process_aec_post_resampling(self, audio_data):
        """
        AEC后重采样到16kHz
        """
        try:
            resampled_data = self.aec_post_resampler.resample_chunk(audio_data, last=False)
            if len(resampled_data) > 0:
                self._resample_aec_post_buffer.extend(resampled_data.astype(np.int16))

            expected_frame_size = AudioConfig.INPUT_FRAME_SIZE
            if len(self._resample_aec_post_buffer) < expected_frame_size:
                return None

            frame_data = []
            for _ in range(expected_frame_size):
                frame_data.append(self._resample_aec_post_buffer.popleft())

            return np.array(frame_data, dtype=np.int16)

        except Exception as e:
            logger.error(f"AEC后重采样失败: {e}")
            return None


    def _put_audio_data_safe(self, queue, audio_data):
        """
        安全入队，队列满时丢弃最旧数据.
        """
        try:
            queue.put_nowait(audio_data)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(audio_data)
            except asyncio.QueueEmpty:
                queue.put_nowait(audio_data)

    def _output_callback(self, outdata: np.ndarray, frames: int, time_info, status):
        """
        播放回调，硬件驱动调用 从播放队列取数据输出到扬声器.
        """
        if status:
            if "underflow" not in str(status).lower():
                logger.warning(f"输出流状态: {status}")

        try:
            if self.output_resampler is not None:
                # 需要重采样：24kHz -> 设备采样率
                self._output_callback_with_resample(outdata, frames)
            else:
                # 直接播放：24kHz
                self._output_callback_direct(outdata, frames)

        except Exception as e:
            logger.error(f"输出回调错误: {e}")
            outdata.fill(0)

    def _output_callback_direct(self, outdata: np.ndarray, frames: int):
        """
        直接播放24kHz数据（设备支持24kHz时）
        """
        try:
            # 从播放队列获取音频数据
            audio_data = self._output_buffer.get_nowait()

            if len(audio_data) >= frames:
                output_frames = audio_data[:frames]
                outdata[:] = output_frames.reshape(-1, AudioConfig.CHANNELS)
            else:
                outdata[: len(audio_data)] = audio_data.reshape(
                    -1, AudioConfig.CHANNELS
                )
                outdata[len(audio_data) :] = 0

        except asyncio.QueueEmpty:
            # 无数据时输出静音
            outdata.fill(0)

    def _output_callback_with_resample(self, outdata: np.ndarray, frames: int):
        """
        重采样播放（24kHz -> 设备采样率）
        """
        try:
            # 持续处理24kHz数据进行重采样
            while len(self._resample_output_buffer) < frames:
                try:
                    audio_data = self._output_buffer.get_nowait()

                    # 24kHz -> 设备采样率重采样
                    resampled_data = self.output_resampler.resample_chunk(
                        audio_data, last=False
                    )
                    if len(resampled_data) > 0:
                        self._resample_output_buffer.extend(
                            resampled_data.astype(np.int16)
                        )

                except asyncio.QueueEmpty:
                    break

            # 从重采样缓冲区取数据
            if len(self._resample_output_buffer) >= frames:
                frame_data = []
                for _ in range(frames):
                    frame_data.append(self._resample_output_buffer.popleft())

                output_array = np.array(frame_data, dtype=np.int16)
                outdata[:] = output_array.reshape(-1, AudioConfig.CHANNELS)
            else:
                # 数据不足时输出静音
                outdata.fill(0)

        except Exception as e:
            logger.warning(f"重采样输出失败: {e}")
            outdata.fill(0)

    def _reference_callback(self, indata, frames, time_info, status):
        """
        参考信号回调 - 照搬quick_realtime_test.py的逻辑，从BlackHole等设备读取系统音频
        """
        if status and "overflow" not in str(status).lower():
            logger.warning(f"参考信号流状态: {status}")

        if self._is_closing:
            return

        try:
            ref_data = indata.copy()
            
            # 转换参考信号为单声道 - 照搬quick_realtime_test.py第136-140行
            if ref_data.ndim == 2:
                ref_mono = np.mean(ref_data, axis=1).astype(np.int16)
            else:
                ref_mono = ref_data.flatten()

            # 重采样到设备采样率（如果需要）
            if self.reference_device_sample_rate != self.device_input_sample_rate:
                # 这里需要重采样处理，但为了简化先直接使用
                # TODO: 添加参考信号重采样
                pass

            # 将参考信号放入缓冲区供AEC使用
            self._add_reference_signal_from_device(ref_mono)

        except Exception as e:
            logger.error(f"参考信号回调错误: {e}")

    def _add_reference_signal_from_device(self, ref_data):
        """
        添加从设备直接读取的参考信号
        """
        try:
            if not self.webrtc_enabled:
                return

            # 限制缓冲区大小
            max_buffer_size = self._device_frame_size * 10  # 最多缓存10帧
            
            # 添加到缓冲区
            self._reference_buffer.extend(ref_data)
            
            # 限制缓冲区大小（避免延迟过大）
            while len(self._reference_buffer) > max_buffer_size:
                self._reference_buffer.popleft()

        except Exception as e:
            logger.warning(f"添加设备参考信号失败: {e}")

    def _input_finished_callback(self):
        """
        输入流结束.
        """
        logger.info("输入流已结束")

    def _reference_finished_callback(self):
        """
        参考信号流结束.
        """
        logger.info("参考信号流已结束")

    def _output_finished_callback(self):
        """
        输出流结束.
        """
        logger.info("输出流已结束")

    async def reinitialize_stream(self, is_input=True):
        """
        重建音频流.
        """
        if self._is_closing:
            return False if is_input else None

        try:
            if is_input:
                if self.input_stream:
                    self.input_stream.stop()
                    self.input_stream.close()

                self.input_stream = sd.InputStream(
                    samplerate=self.device_input_sample_rate,
                    channels=AudioConfig.CHANNELS,
                    dtype=np.int16,
                    blocksize=self._device_input_frame_size,
                    callback=self._input_callback,
                    finished_callback=self._input_finished_callback,
                    latency="low",
                )
                self.input_stream.start()
                logger.info("输入流重新初始化成功")
                return True
            else:
                if self.output_stream:
                    self.output_stream.stop()
                    self.output_stream.close()

                # 根据设备支持的采样率选择输出采样率
                if self.device_output_sample_rate == AudioConfig.OUTPUT_SAMPLE_RATE:
                    # 设备支持24kHz，直接使用
                    output_sample_rate = AudioConfig.OUTPUT_SAMPLE_RATE
                    device_output_frame_size = AudioConfig.OUTPUT_FRAME_SIZE
                else:
                    # 设备不支持24kHz，使用设备默认采样率并启用重采样
                    output_sample_rate = self.device_output_sample_rate
                    device_output_frame_size = int(
                        self.device_output_sample_rate
                        * (AudioConfig.FRAME_DURATION / 1000)
                    )

                self.output_stream = sd.OutputStream(
                    samplerate=output_sample_rate,
                    channels=AudioConfig.CHANNELS,
                    dtype=np.int16,
                    blocksize=device_output_frame_size,
                    callback=self._output_callback,
                    finished_callback=self._output_finished_callback,
                    latency="low",
                )
                self.output_stream.start()
                logger.info("输出流重新初始化成功")
                return None
        except Exception as e:
            stream_type = "输入" if is_input else "输出"
            logger.error(f"{stream_type}流重建失败: {e}")
            if is_input:
                return False
            else:
                raise

    async def get_raw_audio_for_detection(self) -> Optional[bytes]:
        """
        获取唤醒词音频数据.
        """
        try:
            if self._wakeword_buffer.empty():
                return None

            audio_data = self._wakeword_buffer.get_nowait()

            if hasattr(audio_data, "tobytes"):
                return audio_data.tobytes()
            elif hasattr(audio_data, "astype"):
                return audio_data.astype("int16").tobytes()
            else:
                return audio_data

        except asyncio.QueueEmpty:
            return None
        except Exception as e:
            logger.error(f"获取唤醒词音频数据失败: {e}")
            return None

    def set_encoded_audio_callback(self, callback):
        """
        设置编码回调.
        """
        self._encoded_audio_callback = callback

        if callback:
            logger.info("启用实时编码")
        else:
            logger.info("禁用编码回调")

    async def write_audio(self, opus_data: bytes):
        """
        解码音频并播放 网络接收的Opus数据 -> 解码24kHz -> AEC参考信号 + 播放队列.
        """
        try:
            # Opus解码为24kHz PCM数据
            pcm_data = self.opus_decoder.decode(
                opus_data, AudioConfig.OUTPUT_FRAME_SIZE
            )

            audio_array = np.frombuffer(pcm_data, dtype=np.int16)

            expected_length = AudioConfig.OUTPUT_FRAME_SIZE * AudioConfig.CHANNELS
            if len(audio_array) != expected_length:
                logger.warning(
                    f"解码音频长度异常: {len(audio_array)}, 期望: {expected_length}"
                )
                return

            # 将播放音频作为AEC参考信号（重采样到设备采样率）
            self._add_reference_signal(audio_array.copy())

            # 放入播放队列
            self._put_audio_data_safe(self._output_buffer, audio_array)

        except opuslib.OpusError as e:
            logger.warning(f"Opus解码失败，丢弃此帧: {e}")
        except Exception as e:
            logger.warning(f"音频写入失败，丢弃此帧: {e}")

    async def wait_for_audio_complete(self, timeout=10.0):
        """
        等待播放完成.
        """
        start = time.time()

        while not self._output_buffer.empty() and time.time() - start < timeout:
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.3)

        if not self._output_buffer.empty():
            output_remaining = self._output_buffer.qsize()
            logger.warning(f"音频播放超时，剩余队列 - 输出: {output_remaining} 帧")

    async def clear_audio_queue(self):
        """
        清空音频队列.
        """
        cleared_count = 0

        queues_to_clear = [
            self._wakeword_buffer,
            self._output_buffer,
        ]

        for queue in queues_to_clear:
            while not queue.empty():
                try:
                    queue.get_nowait()
                    cleared_count += 1
                except asyncio.QueueEmpty:
                    break

        if self._resample_aec_post_buffer:
            cleared_count += len(self._resample_aec_post_buffer)
            self._resample_aec_post_buffer.clear()

        if self._resample_output_buffer:
            cleared_count += len(self._resample_output_buffer)
            self._resample_output_buffer.clear()

        # 清空AEC参考信号缓冲区
        if self._reference_buffer:
            cleared_count += len(self._reference_buffer)
            self._reference_buffer.clear()



        await asyncio.sleep(0.01)

        if cleared_count > 0:
            logger.info(f"清空音频队列，丢弃 {cleared_count} 帧音频数据")

        if cleared_count > 100:
            gc.collect()
            logger.debug("执行垃圾回收以释放内存")

    async def start_streams(self):
        """
        启动音频流.
        """
        try:
            if self.input_stream and not self.input_stream.active:
                try:
                    self.input_stream.start()
                except Exception as e:
                    logger.warning(f"启动输入流时出错: {e}")
                    await self.reinitialize_stream(is_input=True)

            if self.output_stream and not self.output_stream.active:
                try:
                    self.output_stream.start()
                except Exception as e:
                    logger.warning(f"启动输出流时出错: {e}")
                    await self.reinitialize_stream(is_input=False)

            logger.info("音频流已启动")
        except Exception as e:
            logger.error(f"启动音频流失败: {e}")

    async def stop_streams(self):
        """
        停止音频流.
        """
        try:
            if self.input_stream and self.input_stream.active:
                self.input_stream.stop()
        except Exception as e:
            logger.warning(f"停止输入流失败: {e}")

        try:
            if self.output_stream and self.output_stream.active:
                self.output_stream.stop()
        except Exception as e:
            logger.warning(f"停止输出流失败: {e}")

        try:
            if self.reference_stream and self.reference_stream.active:
                self.reference_stream.stop()
        except Exception as e:
            logger.warning(f"停止参考信号流失败: {e}")

    async def _cleanup_resampler(self, resampler, name):
        """
        清理重采样器.
        """
        if resampler:
            try:
                if hasattr(resampler, "resample_chunk"):
                    empty_array = np.array([], dtype=np.int16)
                    resampler.resample_chunk(empty_array, last=True)
            except Exception as e:
                logger.warning(f"清理{name}重采样器失败: {e}")

    async def close(self):
        """
        关闭音频编解码器.
        """
        if self._is_closing:
            return

        self._is_closing = True
        logger.info("开始关闭音频编解码器...")

        try:
            await self.clear_audio_queue()

            if self.input_stream:
                try:
                    self.input_stream.stop()
                    self.input_stream.close()
                except Exception as e:
                    logger.warning(f"关闭输入流失败: {e}")
                finally:
                    self.input_stream = None

            if self.output_stream:
                try:
                    self.output_stream.stop()
                    self.output_stream.close()
                except Exception as e:
                    logger.warning(f"关闭输出流失败: {e}")
                finally:
                    self.output_stream = None

            if self.reference_stream:
                try:
                    self.reference_stream.stop()
                    self.reference_stream.close()
                except Exception as e:
                    logger.warning(f"关闭参考信号流失败: {e}")
                finally:
                    self.reference_stream = None

            await self._cleanup_resampler(self.aec_post_resampler, "AEC后")
            await self._cleanup_resampler(self.output_resampler, "输出")
            await self._cleanup_resampler(self.reference_resampler, "参考信号")
            self.aec_post_resampler = None
            self.output_resampler = None
            self.reference_resampler = None

            self._resample_aec_post_buffer.clear()
            self._resample_output_buffer.clear()
            self._reference_buffer.clear()

            # 清理WebRTC资源
            if self.webrtc_enabled and self.webrtc_apm is not None:
                try:
                    if self.webrtc_capture_config:
                        self.webrtc_apm.destroy_stream_config(self.webrtc_capture_config)
                    if self.webrtc_render_config:
                        self.webrtc_apm.destroy_stream_config(self.webrtc_render_config)
                except Exception as e:
                    logger.warning(f"清理WebRTC配置失败: {e}")
                finally:
                    self.webrtc_apm = None
                    self.webrtc_capture_config = None
                    self.webrtc_render_config = None
                    self.webrtc_enabled = False

            self.opus_encoder = None
            self.opus_decoder = None


            gc.collect()

            logger.info("音频资源已完全释放")
        except Exception as e:
            logger.error(f"关闭音频编解码器过程中发生错误: {e}")

    def __del__(self):
        """
        析构函数.
        """
        if not self._is_closing:
            logger.warning("AudioCodec未正确关闭，请调用close()")