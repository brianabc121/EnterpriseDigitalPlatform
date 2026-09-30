from pydantic import BaseModel, Field


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=3, max_length=120)
    size: int = Field(gt=0, description="字节数")


class UploadOut(BaseModel):
    """用 PUT 把文件上传到 upload_url（Content-Type 与申请时一致），上传后在消息里引用 file_url。"""

    upload_url: str
    file_url: str
    kind: str = Field(description="image 或 file")
    expires_in: int
