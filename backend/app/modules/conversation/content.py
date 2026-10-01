"""平台保存的消息内容与 OpenIM 消息格式之间的转换。"""

from typing import Any

from app.integrations.openim import ContentType
from app.modules.conversation.models import Message


def im_payload(message: Message) -> tuple[int, dict[str, Any]]:
    """按平台保存的消息生成 OpenIM 的消息类型和内容。"""
    content = message.content
    if message.content_type == "image":
        picture = {
            "uuid": str(message.id),
            "type": content.get("mime") or "",
            "size": content.get("size") or 0,
            "width": content.get("width") or 0,
            "height": content.get("height") or 0,
            "url": content["url"],
        }
        return ContentType.PICTURE, {
            "sourcePicture": picture,
            "bigPicture": picture,
            "snapshotPicture": picture,
        }
    if message.content_type in ("file", "voice", "video") and content.get("url"):
        # 语音、视频（来自微信客服）在服务群里以文件形式展示。
        return ContentType.FILE, {
            "uuid": str(message.id),
            "sourceUrl": content["url"],
            "fileName": content.get("name") or "file",
            "fileSize": content.get("size") or 0,
            "fileType": content.get("mime") or "",
        }
    if message.content_type == "email":
        # 服务群里以文字显示邮件：主题和正文（工作台按平台接口里的内容显示邮件格式）。
        subject = str(content.get("subject") or "")
        body = str(content.get("text") or "")
        return ContentType.TEXT, {"content": f"[邮件] {subject}\n{body}".strip()}
    return ContentType.TEXT, {"content": message.text_plain or ""}
