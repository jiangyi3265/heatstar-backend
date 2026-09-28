#!/usr/bin/env python3
"""提案的原子写入与权限校验。

原来的做法是客户端把整张提案表 PUT 上来整体替换，两个人同时提交时
后写的会把先写的直接抹掉。这里改成服务端按提案编号逐条合并，
并在服务端复核审核权限，避免绕过前端直接改状态。
"""

from __future__ import annotations

import re
import organization


PENDING_BY_LEVEL = {"team": "pending_team", "reviewer": "pending_review", "company": "pending_company"}
LEVEL_BY_PENDING = {v: k for k, v in PENDING_BY_LEVEL.items()}
LEVEL_LABEL = {"team": "班组长", "reviewer": "部门/制造厂评审小组", "company": "公司审核"}
LEVELS_BY_SCOPE = {
    "company": ("company",),
    "reviewer": ("reviewer",),
    "team": ("team",),
}
PROPOSAL_TYPES = {"lean", "six", "tool", "opl"}
# 审核动作允许把提案推向的下一个状态
NEXT_STATUS = {
    "pending_team": {"pending_review", "rejected"},
    "pending_review": {"approved", "rejected"},
}
FINAL_STATUS = {"approved", "rejected"}


class ProposalError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def proposal_id(item) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("id") or item.get("sourceNo") or "").strip()


def is_owner(user: dict, item: dict) -> bool:
    if not item:
        return False
    submitter = str(item.get("submitterId") or item.get('authorWorkNo') or "").strip()
    if submitter and submitter == str(user.get("workNo")):
        return True
    if submitter:
        return False
    # 老数据可能没有 submitterId，退回按姓名认领
    return organization.department_id(user) == organization.department_id(item) and bool(user.get("name")) and item.get("firstAuthor") == user.get("name")


def in_scope(user: dict, item: dict, stage=None) -> bool:
    return organization.in_scope(user, item, stage)


def can_review_at(user: dict, level: str) -> bool:
    if user.get('isSuper'):
        return level in PENDING_BY_LEVEL
    return level in user.get('reviewLevels', LEVELS_BY_SCOPE.get(user.get("reviewLevel") or "", ()))


def validate_structured_opl_steps(item: dict) -> None:
    """新客户端提交的 OPL 步骤必须严格保持一图一说明；无该字段的历史记录继续兼容。"""
    if item.get("type") != "opl" or "oplSteps" not in item:
        return
    steps = item.get("oplSteps")
    if not isinstance(steps, list) or not steps:
        raise ProposalError("OPL 至少需要一个课程步骤")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ProposalError(f"OPL 步骤 {index} 格式不正确")
        photo = str(step.get("photo") or "").strip()
        text = str(step.get("text") or "").strip()
        if not photo or not text:
            raise ProposalError(f"OPL 步骤 {index} 必须包含一张图片和一段说明")


def check_write(user: dict, old: dict | None, new: dict) -> None:
    """在服务端复核这次写入是否被允许。"""
    validate_structured_opl_steps(new)
    new_status = str(new.get("status") or "").strip()

    if old is None:
        # 新建：只能提交自己的提案，且必须从班组级开始走
        if str(new.get("type") or "").strip() not in PROPOSAL_TYPES:
            raise ProposalError("不支持的提交门类", 400)
        if not is_owner(user, new):
            raise ProposalError("只能提交本人的提案", 403)
        if new_status and new_status != "pending_team":
            raise ProposalError("新提案必须从班组长审核开始", 400)
        return

    old_status = str(old.get("status") or "").strip()

    for key in ('type', 'submitterId', 'firstAuthor'):
        if key in old and new.get(key, old[key]) != old[key]:
            raise ProposalError('不能变更提案类型或提交人', 403)

    if old_status == new_status:
        # 没有流转，属于内容修改：本人改自己的，或审核人在范围内补充
        if is_owner(user, old) and old_status == 'rejected':
            return
        raise ProposalError("提案已提交或被其他评审员处理，请刷新后重试", 409)

    # 被驳回后重新提交：本人可以把状态改回班组级
    if old_status == "rejected" and new_status == "pending_team":
        if not is_owner(user, new):
            raise ProposalError("只有提交人本人可以重新提交", 403)
        return

    # 其余状态变化都属于审核动作
    allowed = NEXT_STATUS.get(old_status)
    if not allowed:
        raise ProposalError("该提案已审核完成，不能再改状态", 409)
    if new_status not in allowed:
        raise ProposalError("不允许的审核流转", 400)

    level = LEVEL_BY_PENDING.get(old_status, "")
    if not can_review_at(user, level):
        raise ProposalError("没有这一级的审核权限", 403)
    if not in_scope(user, old, LEVEL_BY_PENDING.get(old.get('status'))):
        raise ProposalError("该提案不在您的管辖范围内", 403)


def merge_one(existing: list, incoming: dict, user: dict, now_text: str = "") -> tuple[list, dict]:
    """把单条提案合并进现有列表，返回新列表和合并后的提案。"""
    key = proposal_id(incoming)
    if not key:
        raise ProposalError("提案缺少编号")

    merged = list(existing)
    index = next((i for i, item in enumerate(merged) if proposal_id(item) == key), -1)
    old = merged[index] if index >= 0 else None

    check_write(user, old, incoming)

    record = dict(old or {})
    record.update(incoming)
    record["id"] = key
    record["updatedBy"] = user.get("name") or user.get("workNo")
    record["updatedByWorkNo"] = user.get("workNo")
    record['workflowVersion'] = 2

    # 时间一律以服务器为准：手机时间不准会把提案算进错误的计分月。
    old_status = str((old or {}).get("status") or "")
    new_status = str(record.get("status") or "")
    record['statusText'] = {'pending_team': '班组长审核', 'pending_review': '评审小组审核', 'approved': '已采纳', 'rejected': '已驳回'}.get(new_status, new_status)
    if new_status != 'approved':
        record['points'] = 0
    if old is not None and old_status != new_status and old_status in LEVEL_BY_PENDING:
        record['reviewer'] = user.get('name') or user.get('workNo')
        record['reviewerWorkNo'] = user.get('workNo')
        stars = int(record.get('stars') or 0)
        if new_status != 'rejected' and stars not in (1, 2, 3):
            raise ProposalError('请选择 1 至 3 星')
        if new_status == 'rejected' and not str(record.get('rejectReason') or '').strip():
            raise ProposalError('驳回必须填写不采纳原因')
    if now_text:
        if old is None:
            record["submittedAt"] = now_text
            record["firstSubmittedAt"] = now_text
        elif old_status == "rejected" and new_status == "pending_team":
            record["submittedAt"] = now_text
        if old is not None and old_status != new_status:
            record["reviewedAt"] = now_text
        record["serverUpdatedAt"] = now_text

    # 审核留痕由服务端接管：以服务端已有记录为准，客户端改不了历史。
    history = [item for item in ((old or {}).get("reviewHistory") or []) if isinstance(item, dict)]
    if old is not None and old_status != new_status and old_status in LEVEL_BY_PENDING:
        level = LEVEL_BY_PENDING[old_status]
        rejected = new_status == "rejected"
        history.append({
            "level": level,
            "levelLabel": LEVEL_LABEL.get(level, ""),
            "reviewer": user.get("name") or user.get("workNo"),
            "reviewerWorkNo": user.get("workNo"),
            "decision": "reject" if rejected else "approve",
            "stars": 0 if rejected else int(record.get("stars") or 0),
            "comment": str((record.get("rejectReason") if rejected else record.get("reviewComment")) or ""),
            "time": now_text or record.get("reviewedAt") or "",
        })
    elif old is not None and old_status == "rejected" and new_status == "pending_team":
        history.append({
            "level": "resubmit",
            "levelLabel": "重新提交",
            "reviewer": user.get("name") or user.get("workNo"),
            "reviewerWorkNo": user.get("workNo"),
            "decision": "resubmit",
            "stars": 0,
            "comment": "",
            "time": now_text or "",
        })
    record["reviewHistory"] = history

    if index >= 0:
        merged[index] = record
    else:
        merged.insert(0, record)
    return merged, record


def sort_table(items) -> list:
    return sorted(items, key=lambda x: str(x.get("submittedAt") or ""), reverse=True)


def merge_table(existing: list, incoming: list, user: dict) -> list:
    """整表 PUT 的兜底：按编号合并而不是整体替换，避免旧快照把别人的提案抹掉。

    只接受调用方有权改动的条目；无权的条目静默保留服务端版本，
    这样老客户端推上来的过期快照不会造成数据丢失。
    合并不做删除，删除请走 remove()。
    """
    by_id = {proposal_id(item): item for item in existing if proposal_id(item)}
    for item in incoming:
        key = proposal_id(item)
        if not key:
            continue
        old = by_id.get(key)
        if old is not None and old == item:
            continue
        # Legacy whole-table writes may not perform review transitions, change identity,
        # or invent history. All new submissions/reviews use the atomic endpoint.
        if old is None or item.get('status') != old.get('status'):
            continue
        try:
            check_write(user, old, item)
        except ProposalError:
            continue
        record = {**old, **item, 'id': key, 'reviewHistory': old.get('reviewHistory') or []}
        for field in ('departmentId', 'department', 'teamId', 'team', 'section', 'stars', 'points', 'reviewer', 'reviewerWorkNo', 'reviewedAt', 'departmentReview', 'recommendation', 'companyRoute', 'finalReviewLevel', 'workflowVersion'):
            if field in old:
                record[field] = old[field]
            else:
                record.pop(field, None)
        by_id[key] = record
    return sort_table(by_id.values())


def remove(existing: list, ids, user: dict) -> tuple[list, list]:
    """按编号删除提案。合并写入永远不会删数据，需要清理时走这里。"""
    if not user.get("isAdmin"):
        raise ProposalError("只有管理员可以删除提案", 403)
    targets = {str(x).strip() for x in ids if str(x).strip()}
    if not targets:
        raise ProposalError("缺少要删除的提案编号")
    kept = []
    removed = []
    for item in existing:
        if proposal_id(item) in targets:
            removed.append(proposal_id(item))
        else:
            kept.append(item)
    return sort_table(kept), removed


def number_prefix(kind: str, states: dict, now, department=None) -> str:
    """各提交门类的编号前缀。"""
    settings = states.get("heatstar_settings") or {}
    code = (department or {}).get('code') or 'RTC'
    suffix = 'RT' if not department or department.get('id') == organization.DEFAULT_DEPARTMENT_ID else code
    if kind == "lean":
        return f"JY-{suffix}-{now.year}-"
    if kind == "tool":
        return f"GJ-{suffix}-{now.year}-"
    if kind == "opl":
        return f"OPL-{suffix}-{now.year}-"
    code = re.sub(r"[^A-Z0-9]", "", str(settings.get("workshopCode") or "RTC").upper())[:8] or "RTC"
    if department and department.get('id') != organization.DEFAULT_DEPARTMENT_ID:
        code = department['code']
    cycle = settings.get("resetCycle") or "monthly"
    if cycle == "quarterly":
        period = f"{now.year}Q{(now.month - 1) // 3 + 1}"
    elif cycle == "yearly":
        period = str(now.year)
    elif cycle == "never":
        period = "ALL"
    else:
        period = f"{now.year}{now.month:02d}"
    return f"{code}-LY-{period}-"


def highest_used(prefix: str, states: dict) -> int:
    """扫一遍现有提案，取该前缀下已用的最大序号，避免与历史数据撞号。"""
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    highest = 0
    for item in states.get("heatstar_proposals") or []:
        if not isinstance(item, dict):
            continue
        for value in (item.get("id"), item.get("sourceNo")):
            match = pattern.match(str(value or ""))
            if match:
                highest = max(highest, int(match.group(1)))
    return highest
