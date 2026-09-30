"""媒体转码：微信客服的 AMR 语音转成 MP3（需要 ffmpeg，没有安装时跳过）。"""

import shutil

import pytest

from app.integrations.media import to_mp3

# 最小的 AMR-NB 文件：文件头加 50 帧（12.2 kbps 模式，每帧 32 字节，共 1 秒）。
AMR = b"#!AMR\n" + (b"\x3c" + b"\x00" * 31) * 50


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
async def test_amr_is_converted_to_mp3() -> None:
    mp3 = await to_mp3(AMR)
    assert mp3 is not None
    assert mp3[:3] == b"ID3" or mp3[0] == 0xFF


async def test_missing_ffmpeg_or_bad_input_keeps_the_original() -> None:
    assert await to_mp3(AMR, ffmpeg="/nonexistent/ffmpeg") is None
    assert await to_mp3(b"") is None
