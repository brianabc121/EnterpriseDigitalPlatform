"""浏览器验收的配置（scripts/ci/e2e.env）要和仓库里的模拟服务一致。

企业微信的服务商凭证不一致时，模拟企业微信推送的 suite_ticket 回调验签失败，CI 里企业微信的
验收全部失败（只在每天夜里的全部验收里才会发现）。
"""

from pathlib import Path

from tests import fake_wecom

E2E_ENV = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "e2e.env"


def test_e2e_env_matches_the_fake_wecom_server() -> None:
    values = dict(
        line.split("=", 1)
        for line in E2E_ENV.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )

    assert values["EDP_WECOM_SUITE_ID"] == fake_wecom.SUITE_ID
    assert values["EDP_WECOM_SUITE_SECRET"] == fake_wecom.SUITE_SECRET
    assert values["EDP_WECOM_TOKEN"] == fake_wecom.TOKEN
    assert values["EDP_WECOM_ENCODING_AES_KEY"] == fake_wecom.AES_KEY
