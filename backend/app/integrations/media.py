"""媒体转码（设计文档 §9.3）：微信客服的语音是 AMR 格式，浏览器不能直接播放，转成 MP3。

依赖 ffmpeg（EDP_FFMPEG_PATH，默认在 PATH 里查找）；没有安装或转码失败时返回 None，
调用方保留原文件（工作台提供下载）。
"""

import asyncio
import logging
import shutil

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
_MAX_INPUT_BYTES = 20 * 1024 * 1024


async def to_mp3(data: bytes, ffmpeg: str = "ffmpeg") -> bytes | None:
    """把音频（AMR、SILK 以外 ffmpeg 能解码的格式）转成 MP3。"""
    binary = shutil.which(ffmpeg)
    if binary is None or not data or len(data) > _MAX_INPUT_BYTES:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "5",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout=_TIMEOUT_SECONDS)
    except (OSError, TimeoutError) as exc:
        logger.warning("ffmpeg transcode failed: %s", exc)
        return None
    if process.returncode != 0 or not stdout:
        logger.warning("ffmpeg transcode failed: %s", stderr.decode(errors="ignore")[:300])
        return None
    return stdout
