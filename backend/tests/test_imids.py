import uuid

from app.modules.conversation import imids


def test_ids_round_trip_for_hyphenated_tenant_codes() -> None:
    room_id = uuid.uuid4()
    group = imids.room_group("acme-co", room_id)

    assert group == f"acmeXco_r_{room_id.hex}"
    assert imids.parse(group) == imids.ParsedId("acme-co", imids.Kind.ROOM, room_id)
    assert imids.parse(imids.system_user("acme-co")) == imids.ParsedId(
        "acme-co", imids.Kind.SYSTEM, None
    )
    identity_id = uuid.uuid4()
    assert imids.parse(imids.customer_user("a-b-c", identity_id)) == imids.ParsedId(
        "a-b-c", imids.Kind.CUSTOMER, identity_id
    )


def test_ids_only_use_characters_openim_accepts() -> None:
    for im_id in (
        imids.customer_user("acme-co", uuid.uuid4()),
        imids.staff_user("acme-co", uuid.uuid4()),
        imids.bot_user("acme-co"),
        imids.system_user("acme-co"),
        imids.room_group("acme-co", uuid.uuid4()),
    ):
        assert all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in im_id), im_id


def test_parse_rejects_ids_outside_the_convention() -> None:
    for im_id in (
        "imAdmin",
        "acme-co_sys",
        "Acme_sys",
        "acme_x_" + uuid.uuid4().hex,
        "acme_r_not-a-uuid",
        "acme_r_" + uuid.uuid4().hex.upper(),
        "ab_sys",
        "acme_evil",
    ):
        assert imids.parse(im_id) is None, im_id
