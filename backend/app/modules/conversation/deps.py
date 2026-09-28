from fastapi import Request

from app.integrations.openim import OpenIMClient
from app.modules.conversation.provisioning import IMProvisioner


def get_im(request: Request) -> OpenIMClient:
    im: OpenIMClient = request.app.state.im
    return im


def get_im_provisioner(request: Request) -> IMProvisioner:
    provisioner: IMProvisioner = request.app.state.im_provisioner
    return provisioner
