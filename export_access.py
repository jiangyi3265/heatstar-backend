"""Proposal export policy shared by preview and fixed document formats."""
import re

import auth
import organization


EXPORT_FORMAT_BY_TYPE = {
    'lean': 'docx',
    'six': 'pptx',
    'tool': 'docx',
    'opl': 'docx',
}


def proposal_export_format(item):
    """Return the only export format allowed for a proposal type."""
    return EXPORT_FORMAT_BY_TYPE.get(str(item.get('type') or ''), 'docx')


def select_format(items, extension):
    return [item for item in items if proposal_export_format(item) == extension]


def visible_proposals(user, states):
    level = 'factory' if user.get('isSuper') else (user.get('reviewLevel') or user.get('accessLevel', ''))
    field = {'team': 'team', 'section': 'section'}.get(level)
    scopes = set()
    if field:
        scopes = {s.strip() for s in re.split(r'[、,，;；\n]', str(user.get('scope') or '')) if s.strip()}
        placeholders = {'本人', '本班组', '班组'} if field == 'team' else {'本人', '本工段', '工段'}
        if not scopes or scopes <= placeholders:
            scopes = {str(user.get(field) or '').strip()} - {''}
    people = states.get('heatstar_people') or []
    work_no = str(user.get('workNo') or '').strip()
    name = str(user.get('name') or '').strip()
    # Old records only stored a name. Resolve it only when it identifies one account.
    same_name = {str(p.get('workNo') or p.get('id') or '').strip()
                 for p in people if isinstance(p, dict) and p.get('name') == name
                 and organization.department_id(p) == organization.department_id(user)}

    def allowed(item):
        if user.get('scopeByLevel'):
            return organization.in_scope(user, item)
        if level in ('factory', 'reviewer', 'company', 'specialist') or field:
            return organization.in_scope(user, item)
        owner = str(item.get('submitterId') or item.get('authorWorkNo') or '').strip()
        if owner:
            return bool(work_no) and owner == work_no
        return (organization.department_id(item) == organization.department_id(user)
                and bool(name) and same_name == {work_no}
                and (item.get('submitterName') or item.get('firstAuthor')) == name)

    label = ('全公司提案' if user.get('isSuper') else f"{user.get('department') or '热处理厂'}提案") if level == 'factory' else ('评审小组：' + (user.get('scope') or user.get('department') or '热处理厂') if level == 'reviewer' else (
        ('班组：' if field == 'team' else '工段：') + ('、'.join(sorted(scopes)) or '未配置管辖范围')
        if field else '本人提交的提案'))
    items = [x for x in states.get('heatstar_proposals', []) if isinstance(x, dict) and allowed(x)]
    if level == 'company':
        label = '全公司提案'
    elif level == 'specialist':
        label = '精益专员：' + (user.get('scope') or user.get('department') or '')
    return items, label


def select_proposals(items, ids):
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x.strip() for x in ids):
        raise ValueError('请至少勾选一条提案')
    wanted = set(ids)
    available = {str(x.get('id')) for x in items}
    if not wanted <= available:
        raise auth.AuthError('所选提案已变化或不在可导出范围内，请刷新后重新选择', 403)
    return [x for x in items if str(x.get('id')) in wanted]
