from fastapi import Request

from app.core.config import Settings
from app.integrations.openim import OpenIMClient
from app.modules.conversation.provisioning import IMProvisioner


def get_im(request: Request) -> OpenIMClient:
    im: OpenIMClient = request.app.state.im
    return im


def get_im_provisioner(request: Request) -> IMProvisioner:
    provisioner: IMProvisioner = request.app.state.im_provisioner
    return provisioner


def openim_from_settings(settings: Settings) -> OpenIMClient:
    return OpenIMClient(
        settings.openim_api_url,
        secret=settings.openim_secret.get_secret_value(),
        admin_user_id=settings.openim_admin_user_id,
    )
