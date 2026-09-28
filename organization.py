"""Department identity and scope helpers. Missing identity denotes the original factory."""
import re

DEFAULT_DEPARTMENT_ID = 'heat-treatment'
DEFAULT_DEPARTMENT_NAME = '热处理厂'


def department_id(item):
    return str((item or {}).get('departmentId') or DEFAULT_DEPARTMENT_ID)


def departments(states):
    return states.get('heatstar_departments') or [
        {'id': DEFAULT_DEPARTMENT_ID, 'name': DEFAULT_DEPARTMENT_NAME, 'code': 'RTC', 'status': '启用'}
    ]


def department_name(item, states):
    return next((d['name'] for d in departments(states) if d['id'] == department_id(item)),
                (item or {}).get('department') or DEFAULT_DEPARTMENT_NAME)


def scopes(user):
    return {s.strip() for s in re.split(r'[、,，;；\n]', str(user.get('scope') or '')) if s.strip()}


def configured_levels(person):
    if isinstance(person.get('levels'), list):
        return person['levels']
    return [person.get('level') or '无']


def configured_roles(person):
    return person['permissionRoles'] if isinstance(person.get('permissionRoles'), list) else [person.get('permissionRole') or '审核者']


def profile_department_scopes(person, states, level):
    if 'levels' in person:
        return (person.get('levelScopes') or {}).get(level, [])
    return person.get('departmentScopes') or [d['id'] for d in departments(states) if d['name'] in scopes(person)] or [department_id(person)]


def in_scope(user, item, stage=None):
    if user.get('isSuper'):
        return True
    if 'scopeByLevel' in user:
        grants = user['scopeByLevel']
        selected_grants = {stage: grants[stage]} if stage in grants else ({} if stage else grants)
        return any(in_scope({**grant, 'reviewLevel': kind}, item) for kind, grant in selected_grants.items())
    level = user.get('reviewLevel') or user.get('accessLevel') or ''
    if level == 'company':
        return True
    selected = scopes(user)
    if level in ('reviewer', 'specialist'):
        selected = set(user.get('departmentScopes') or []) or {department_id(user)}
        return department_id(item) in selected
    if department_id(item) != department_id(user):
        return False
    if level == 'factory':
        return True
    field = {'team': 'team', 'section': 'section'}.get(level)
    if not field:
        return False
    placeholders = {'本人', '本班组', '班组', '本工段', '工段'}
    if not selected or selected <= placeholders:
        selected = {str(user.get(field) or '')} - {''}
    return item.get(field) in selected


def validate_departments(value, states):
    if not isinstance(value, list) or not value:
        raise ValueError('至少保留一个部门')
    ids, names, codes = set(), set(), set()
    for d in value:
        if not isinstance(d, dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', str(d.get('id') or '')):
            raise ValueError('部门标识无效')
        name, code = str(d.get('name') or '').strip(), str(d.get('code') or '').strip()
        if not name or len(name) > 40 or not re.fullmatch(r'[A-Z0-9]{2,8}', code):
            raise ValueError('请输入部门名称及 2 至 8 位大写字母或数字编号代码')
        if d['id'] in ids or name in names or code in codes:
            raise ValueError('部门名称、标识和编号代码不能重复')
        if d.get('status') not in ('启用', '停用'):
            raise ValueError('部门状态无效')
        ids.add(d['id']); names.add(name); codes.add(code)
    referenced = {DEFAULT_DEPARTMENT_ID}
    for key in ('heatstar_people', 'heatstar_teams', 'heatstar_proposals'):
        referenced.update(department_id(item) for item in states.get(key, []) if isinstance(item, dict))
    if not referenced <= ids:
        raise ValueError('有人员、班组或历史提案的部门不能删除，请改为停用')


def normalize_people(people, states):
    if not isinstance(people, list):
        raise ValueError('人员名单格式无效')
    teams = states.get('heatstar_teams') or []
    known = {d['id'] for d in departments(states)}
    result = []
    for person in people:
        item = dict(person)
        if not item.get('departmentId') and item.get('department'):
            match = next((d for d in departments(states) if d['name'] == item['department']), None)
            if not match:
                raise ValueError('人员所属部门不存在')
            item['departmentId'] = match['id']
        did = department_id(item)
        if did not in known:
            raise ValueError('人员所属部门不存在')
        if item.get('departmentScopes') and (not isinstance(item['departmentScopes'], list) or not set(item['departmentScopes']) <= known):
            raise ValueError('评审员管辖部门不存在')
        if 'levels' in item:
            levels = item['levels']
            allowed = {'班组', '评审员', '部门', '公司', '精益专员', '工段'}
            if not isinstance(levels, list) or any(not isinstance(v, str) or v not in allowed for v in levels) or len(set(levels)) != len(levels):
                raise ValueError('审核职责格式无效')
            scope_map = item.get('levelScopes')
            if not isinstance(scope_map, dict) or set(scope_map) != set(levels):
                raise ValueError('请分别配置每项职责的管辖范围')
            for level in levels:
                selected = scope_map[level]
                if not isinstance(selected, list) or not selected or any(not isinstance(v, str) for v in selected) or len(set(selected)) != len(selected):
                    raise ValueError('每项职责至少选择一个有效范围')
                valid = known if level in ('评审员', '精益专员') else {did} if level == '部门' else {'*'} if level == '公司' else {
                    str(t.get('name' if level == '班组' else 'section') or '') for t in teams if department_id(t) == did} - {''}
                if not set(selected) <= valid:
                    raise ValueError('审核职责的管辖范围不存在或不属于该部门')
        if 'permissionRoles' in item:
            roles = item['permissionRoles']
            if not isinstance(roles, list) or not roles or any(not isinstance(r, str) or r not in ('员工','审核者','管理员') for r in roles) or len(set(roles)) != len(roles):
                raise ValueError('功能角色格式无效')
        team = next((t for t in teams if department_id(t) == did and
                     (t.get('id') == item.get('teamId') if item.get('teamId') else t.get('name') == item.get('team'))), None)
        item.update(departmentId=did, department=department_name(item, states))
        if team:
            item.update(teamId=team['id'], team=team['name'], section=team.get('section') or '')
        result.append(item)
    return result


def submission(incoming, old, user, states):
    item = dict(incoming)
    did = department_id(old or user)
    if department_id(item) != did and item.get('departmentId'):
        raise ValueError('不能跨部门提交或变更提案归属')
    item['departmentId'] = did
    item['department'] = department_name(item, states)
    if old:
        for key in ('teamId', 'team', 'section'):
            if key in old and item.get(key, old.get(key)) != old.get(key):
                raise ValueError('已提交提案不能变更组织归属')
        if 'teamId' not in old and item.get('teamId'):
            valid = any(t.get('id') == item['teamId'] and t.get('name') == old.get('team') and department_id(t) == did for t in states.get('heatstar_teams', []))
            if not valid:
                raise ValueError('已提交提案不能变更班组归属')
    else:
        dep = next((d for d in departments(states) if d['id'] == did), None)
        if not dep or dep.get('status') == '停用':
            raise ValueError('所属部门已停用，不能提交新提案')
        # A submitter cannot route a proposal into another department's same-name team.
        teams = [t for t in states.get('heatstar_teams', []) if department_id(t) == did]
        team = next((t for t in teams if t.get('id') == item.get('teamId')), None) if item.get('teamId') else next((t for t in teams if t.get('name') == item.get('team')), None)
        if not team:
            raise ValueError('请选择所属部门内的有效班组')
        item.update(teamId=team['id'], team=team['name'], section=team.get('section') or '')
    return item
