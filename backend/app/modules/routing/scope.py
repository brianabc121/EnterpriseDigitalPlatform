"""团队数据范围：主管（组长）能看到所带技能组的会话、组员的客户和会话。"""

from uuid import UUID

from sqlalchemy import Select, select

from app.modules.routing.models import SkillGroupMember


def led_groups(staff_id: UUID) -> Select[UUID]:
    """staff_id 担任组长的技能组。"""
    return select(SkillGroupMember.skill_group_id).where(
        SkillGroupMember.staff_id == staff_id, SkillGroupMember.is_lead
    )


def team_members(staff_id: UUID) -> Select[UUID]:
    """staff_id 所带技能组的全部成员（包括自己）。"""
    return select(SkillGroupMember.staff_id).where(
        SkillGroupMember.skill_group_id.in_(led_groups(staff_id))
    )
