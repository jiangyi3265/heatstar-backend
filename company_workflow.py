"""Department ballots and company escalation. Called under app.WRITE_LOCK."""
import math

import organization
import proposals

PROTECTED = ('departmentReview', 'recommendation', 'companyRoute', 'finalReviewLevel')
BLOCKED = {'已移除', '离职', '离岗', '停用', '禁用'}


def members_for(item, states):
    members = {}
    for person in states.get('heatstar_people', []):
        if '评审员' not in organization.configured_levels(person) or not any(r in organization.configured_roles(person) for r in ('审核者','管理员')) or person.get('status') in BLOCKED:
            continue
        scopes = organization.profile_department_scopes(person, states, '评审员')
        work_no = str(person.get('workNo') or person.get('id') or '').strip()
        if work_no and organization.department_id(item) in scopes:
            members[work_no] = {'workNo': work_no, 'name': person.get('name') or work_no}
    return list(members.values())


def new_ballot(item, states):
    return {'members': members_for(item, states), 'votes': [], 'completed': False}


def history_entry(user, level, decision, stars, comment, now):
    return {'level': level, 'levelLabel': {'reviewer':'部门/制造厂评审小组','company':'公司审核','specialist':'精益专员推荐'}[level],
            'reviewer': user.get('name') or user.get('workNo'), 'reviewerWorkNo': user.get('workNo'),
            'decision': decision, 'stars': stars, 'comment': comment, 'time': now}


def merge_one(existing, incoming, user, states, now):
    key = proposals.proposal_id(incoming)
    index = next((i for i, p in enumerate(existing) if proposals.proposal_id(p) == key), -1)
    old = existing[index] if index >= 0 else None
    action = incoming.get('reviewAction')
    if action not in (None, '', 'recommend'):
        raise proposals.ProposalError('不支持的评审动作')
    if action == 'recommend' or (old and old.get('status') in ('pending_review', 'pending_company')):
        if not old:
            raise proposals.ProposalError('提案不存在', 404)
        stage = 'specialist' if action == 'recommend' else 'reviewer' if old['status'] == 'pending_review' else 'company'
        if not organization.in_scope(user, old, stage):
            raise proposals.ProposalError('该提案不在您的管辖范围内', 403)
        for field in ('type', 'submitterId', 'firstAuthor'):
            if field in incoming and incoming[field] != old.get(field):
                raise proposals.ProposalError('不能变更提案类型或提交人', 403)
        record = dict(old)
        history = list(old.get('reviewHistory') or [])
        if action == 'recommend':
            if not user.get('canRecommend') and not user.get('isSuper'):
                raise proposals.ProposalError('只有精益专员可以推荐到公司审核', 403)
            ballot = old.get('departmentReview') or {}
            if old.get('status') != 'approved' or old.get('finalReviewLevel') != 'department' or not ballot.get('completed'):
                raise proposals.ProposalError('须先完成部门小组评审并在部门结案，才能推荐', 409)
            reason = str(incoming.get('recommendationReason') or '').strip()
            if not reason or len(reason) > 300:
                raise proposals.ProposalError('请填写 1 至 300 字的推荐原因')
            record.update(status='pending_company', companyRoute='specialist', finalReviewLevel='', stars=0, points=0,
                          recommendation={'workNo':user.get('workNo'), 'name':user.get('name'), 'reason':reason, 'time':now})
            history.append(history_entry(user, 'specialist', 'recommend', 0, reason, now))
        else:
            level = 'reviewer' if old['status'] == 'pending_review' else 'company'
            if not proposals.can_review_at(user, level):
                raise proposals.ProposalError('没有这一级的审核权限', 403)
            target = incoming.get('status')
            if target not in ('approved', 'rejected'):
                raise proposals.ProposalError('不允许的审核流转')
            rejected = target == 'rejected'
            try:
                raw = incoming.get('stars')
                stars = 0 if rejected else int(raw)
                if not rejected and (stars not in (1, 2, 3) or float(raw) != stars):
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                raise proposals.ProposalError('请选择 1 至 3 星')
            comment = str(incoming.get('rejectReason' if rejected else 'reviewComment') or '').strip()
            if rejected and not comment:
                raise proposals.ProposalError('驳回必须填写不采纳原因')
            if level == 'reviewer':
                ballot = dict(old.get('departmentReview') or {})
                if not ballot.get('members'):
                    ballot = new_ballot(old, states)
                work_no = str(user.get('workNo') or '')
                if work_no not in {m['workNo'] for m in ballot['members']}:
                    raise proposals.ProposalError('不在本轮部门小组名单内，不能代替小组成员评分', 403)
                votes = list(ballot.get('votes') or [])
                if any(v['workNo'] == work_no for v in votes):
                    raise proposals.ProposalError('您已完成本轮评分，不能重复评分', 409)
                votes.append({'workNo':work_no, 'name':user.get('name'), 'stars':stars, 'decision':'reject' if rejected else 'approve', 'comment':comment, 'time':now})
                n = len(ballot['members'])
                total = sum(v['stars'] for v in votes)
                three = sum(v['stars'] == 3 for v in votes)
                complete = len(votes) == n and not rejected
                ballot.update(votes=votes, totalScore=total, maxScore=n * 3, threeStarCount=three,
                              requiredThreeStar=math.ceil(n * .8), completed=complete,
                              averageStars=total // n if complete else None, completedAt=now if complete else '')
                record.update(departmentReview=ballot, stars=0, points=0, finalReviewLevel='')
                if rejected:
                    record.update(status='rejected', rejectReason=comment)
                elif complete:
                    # Integer arithmetic: exact 80% threshold, not total score / maximum.
                    escalate = three * 5 >= n * 4
                    record.update(status='pending_company' if escalate else 'approved',
                                  stars=0 if escalate else total // n,
                                  finalReviewLevel='' if escalate else 'department',
                                  companyRoute='threshold' if escalate else '')
                else:
                    record['status'] = 'pending_review'
            else:
                record.update(status=target, stars=stars, finalReviewLevel='company',
                              rejectReason=comment if rejected else '', reviewComment='' if rejected else comment)
            history.append(history_entry(user, level, 'reject' if rejected else 'approve', stars, comment, now))
            record.update(reviewer=user.get('name'), reviewerWorkNo=user.get('workNo'))
        record.update(reviewHistory=history, updatedBy=user.get('name'), updatedByWorkNo=user.get('workNo'),
                      serverUpdatedAt=now, workflowVersion=3)
        if record['status'] in ('approved', 'rejected'):
            record['reviewedAt'] = now
        else:
            record['reviewedAt'] = ''
        merged = list(existing)
        merged[index] = record
    else:
        safe = {k:v for k,v in incoming.items() if k not in (*PROTECTED, 'reviewAction', 'recommendationReason')}
        if old and old.get('status') == 'pending_team':
            # A reviewer may supply a decision, never rewrite the proposal's content.
            safe = {**old, **{k:safe[k] for k in ('status','stars','reviewComment','rejectReason') if k in safe}}
        merged, record = proposals.merge_one(existing, safe, user, now)
        if old is None or (old.get('status') == 'rejected' and record['status'] == 'pending_team'):
            for field in PROTECTED:
                record.pop(field, None)
            record.update(stars=0, points=0)
        if record['status'] == 'pending_review' and (not old or old.get('status') != 'pending_review'):
            record['departmentReview'] = new_ballot(record, states)
        record['workflowVersion'] = 3
    record['statusText'] = {'pending_team':'班组长审核','pending_review':'部门/制造厂小组评审','pending_company':'公司审核',
                            'approved':'已采纳','rejected':'已驳回'}[record['status']]
    settings = states.get('heatstar_settings') or {}
    rules = settings.get('pointRules') or settings
    record['points'] = {1:rules.get('oneStar',1), 2:rules.get('twoStar',3), 3:rules.get('threeStar',5)}.get(record.get('stars'), 0) if record['status'] == 'approved' else 0
    return merged, record
