import http.client
import json
import tempfile
import threading
import unittest
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import os

os.environ.setdefault('HEATSTAR_SUPER_ADMIN_PASSWORD', 'test-super-admin-password')
os.environ.setdefault('HEATSTAR_DEFAULT_PASSWORD', 'test-user-initial-password')

import app


class HeatStarApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="heatstar-api-test-")
        app.DATA_DIR = Path(cls.temp.name)
        app.UPLOAD_DIR = app.DATA_DIR / "uploads"
        app.DB_PATH = app.DATA_DIR / "heatstar.sqlite3"
        app.initialize_database()
        states, _, _ = app.get_states()
        app.set_state('heatstar_people', states['heatstar_people'] + [
            {'id':'QA-R1','workNo':'QA-R1','name':'评审甲','team':'行政组','section':'厂级职能','role':'评审员','level':'评审员','scope':'热处理厂','status':'在岗'},
            {'id':'QA-R2','workNo':'QA-R2','name':'评审乙','team':'行政组','section':'厂级职能','role':'员工','level':'评审员','scope':'热处理厂','status':'在岗'},
        ])
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.HeatStarHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.token = app.auth.login('admin', app.auth.SUPER_ADMIN_DEFAULT_PASSWORD)['token']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers={"Authorization": f"Bearer {self.token}", **(headers or {})})
        response = connection.getresponse()
        payload = response.read()
        result_headers = dict(response.getheaders())
        connection.close()
        return response.status, result_headers, payload

    def json_request(self, method, path, value=None):
        body = None if value is None else json.dumps(value).encode("utf-8")
        status, headers, payload = self.request(method, path, body, {"Content-Type": "application/json"})
        return status, headers, json.loads(payload.decode("utf-8"))

    def multipart(self, filename, data, content_type):
        boundary = "----heatstar-test-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        return body, {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))}

    def test_health_bootstrap_and_cors(self):
        status, headers, payload = self.request("GET", "/api/health", headers={"Origin": "http://localhost:4173"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://localhost:4173")
        self.assertTrue(json.loads(payload.decode("utf-8"))["ok"])
        status, _, bootstrap = self.json_request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(len(bootstrap["states"]), 11)

    def test_state_roundtrip(self):
        value = [{"id": "QA", "title": "同步测试", "status": "草稿"}]
        status, _, result = self.json_request("PUT", "/api/state/heatstar_notices", {"value": value})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(result["revision"], 2)
        status, _, stored = self.json_request("GET", "/api/state/heatstar_notices")
        self.assertEqual(status, 200)
        self.assertEqual(stored["value"], value)

    def test_server_number_is_unique(self):
        _, _, first = self.json_request("POST", "/api/numbers/six-source", {})
        _, _, second = self.json_request("POST", "/api/numbers/six-source", {})
        self.assertNotEqual(first["number"], second["number"])
        self.assertTrue(first["number"].startswith("RTC-LY-"))
        for kind, prefix in [('lean', 'JY-RT-'), ('tool', 'GJ-RT-'), ('opl', 'OPL-RT-')]:
            status, _, result = self.json_request("POST", f"/api/numbers/proposal?type={kind}", {})
            self.assertEqual(status, 201)
            self.assertTrue(result['number'].startswith(prefix))
        status, _, _ = self.json_request("POST", "/api/numbers/proposal?type=unknown", {})
        self.assertEqual(status, 400)

    def test_xlsx_template_and_preview(self):
        status, _, workbook = self.request("GET", "/api/templates/personnel.xlsx")
        self.assertEqual(status, 200)
        body, headers = self.multipart("people.xlsx", workbook, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        status, _, payload = self.request("POST", "/api/import/personnel/preview", body, headers)
        parsed = json.loads(payload.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(parsed["rows"][0]["workNo"], "60821")

    def test_photo_upload_and_download(self):
        body, headers = self.multipart("photo.png", b"\x89PNG\r\n\x1a\nheatstar", "image/png")
        status, _, payload = self.request("POST", "/api/upload?kind=photo", body, headers)
        uploaded = json.loads(payload.decode("utf-8"))["file"]
        self.assertEqual(status, 201)
        status, _, downloaded = self.request("GET", f"/api{uploaded['url']}")
        self.assertEqual(status, 200)
        self.assertEqual(downloaded, b"\x89PNG\r\n\x1a\nheatstar")

    def test_photo_upload_accepts_more_than_two_mb_and_caps_at_twenty_mb(self):
        large_photo = b"\xff\xd8" + b"x" * (3 * 1024 * 1024)
        body, headers = self.multipart("large.jpg", large_photo, "image/jpeg")
        status, _, payload = self.request("POST", "/api/upload?kind=photo", body, headers)
        self.assertEqual(status, 201, payload[:200])

        class Oversize:
            def __len__(self):
                return 20 * 1024 * 1024 + 1

        with self.assertRaisesRegex(ValueError, "20MB"):
            app.store_upload({"data": Oversize(), "content_type": "image/jpeg", "filename": "too-large.jpg"}, "photo")

    def test_unauthenticated_and_employee_permissions(self):
        status, _, _ = self.request('GET', '/api/bootstrap', headers={'Authorization': ''})
        self.assertEqual(status, 401)
        token = app.auth.login('56348', app.auth.DEFAULT_PASSWORD)['token']
        for path in ['/api/auth/accounts', '/api/templates/personnel.xlsx']:
            status, _, _ = self.request('GET', path, headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(status, 403)
        status, _, _ = self.request('PUT', '/api/state/heatstar_settings', b'{"value":{}}', {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)

    def test_every_logged_in_employee_can_export(self):
        token = app.auth.login('56348', app.auth.DEFAULT_PASSWORD)['token']
        headers = {'Authorization': f'Bearer {token}'}
        for path in ['/api/export/proposals/preview', '/api/export/proposals.docx', '/api/export/proposals.pptx']:
            status, _, _ = self.request('GET', path, headers={'Authorization': ''})
            self.assertEqual(status, 401)
        status, _, data = self.request('GET', '/api/export/proposals/preview', headers=headers)
        self.assertEqual(status, 200)
        preview = json.loads(data)
        self.assertGreater(preview['count'], 0)
        self.assertEqual(preview['accessScope'], '本人提交的提案')
        six_selected = next(item for item in preview['items'] if item['type'] == 'six')
        word_selected = next(item for item in preview['items'] if item['type'] != 'six')
        self.assertEqual(six_selected['exportFormat'], 'pptx')
        self.assertEqual(word_selected['exportFormat'], 'docx')
        for selected, extension, main in [
            (word_selected, 'docx', 'word/document.xml'),
            (six_selected, 'pptx', 'ppt/presentation.xml'),
        ]:
            body = json.dumps({'ids': [selected['id']]}).encode('utf-8')
            status, _, data = self.request('POST', '/api/export/proposals.' + extension, body,
                                           {**headers, 'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertIn(main, archive.namelist())
                self.assertIsNone(archive.testzip())

        wrong = json.dumps({'ids': [six_selected['id']]}).encode('utf-8')
        status, _, data = self.request('POST', '/api/export/proposals.docx', wrong,
                                       {**headers, 'Content-Type': 'application/json'})
        self.assertEqual(status, 400)
        self.assertIn('固定使用 PPT', json.loads(data)['message'])

        status, _, admin_data = self.request('GET', '/api/export/proposals/preview')
        outside = next(item['id'] for item in json.loads(admin_data)['items']
                       if item['id'] not in {x['id'] for x in preview['items']})
        invalid = json.dumps({'ids': [word_selected['id'], outside]}).encode('utf-8')
        status, _, _ = self.request('POST', '/api/export/proposals.docx', invalid,
                                    {**headers, 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)

    def test_export_scope_policy(self):
        states = {
            'heatstar_people': [
                {'workNo': 'E1', 'name': '员工甲'}, {'workNo': 'E2', 'name': '同名'},
                {'workNo': 'E3', 'name': '同名'}
            ],
            'heatstar_proposals': [
                {'id': 'A', 'submitterId': 'E1', 'firstAuthor': '员工甲', 'team': '甲班', 'section': '一段'},
                {'id': 'B', 'submitterId': 'E2', 'firstAuthor': '同名', 'team': '乙班', 'section': '一段'},
                {'id': 'C', 'firstAuthor': '同名', 'team': '丙班', 'section': '二段'},
                {'id': 'D', 'firstAuthor': '员工甲', 'team': '丁班', 'section': '二段'},
            ]
        }
        cases = [
            ({'workNo': 'E1', 'name': '员工甲', 'reviewLevel': ''}, {'A', 'D'}),
            ({'workNo': 'T', 'name': '班长', 'reviewLevel': 'team', 'scope': '甲班、丙班'}, {'A', 'C'}),
            ({'workNo': 'T2', 'name': '班长乙', 'reviewLevel': 'team', 'scope': '班组', 'team': '乙班'}, {'B'}),
            ({'workNo': 'S', 'name': '段长', 'reviewLevel': 'section', 'scope': '二段'}, {'C', 'D'}),
            ({'workNo': 'S2', 'name': '段长乙', 'reviewLevel': 'section', 'scope': '工段', 'section': '一段'}, {'A', 'B'}),
            ({'workNo': 'F', 'name': '厂长', 'reviewLevel': 'factory'}, {'A', 'B', 'C', 'D'}),
            ({'workNo': 'E2', 'name': '同名', 'reviewLevel': ''}, {'B'}),
        ]
        for user, expected in cases:
            visible, _ = app.export_access.visible_proposals(user, states)
            self.assertEqual({x['id'] for x in visible}, expected)

    def test_export_formats(self):
        for extension, main in [('docx', 'word/document.xml'), ('pptx', 'ppt/presentation.xml')]:
            status, _, data = self.request('GET', '/api/export/proposals.' + extension)
            self.assertEqual(status, 200)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertIn(main, archive.namelist())
                self.assertIsNone(archive.testzip())

    def test_export_format_mapping_is_fixed(self):
        cases = {
            'lean': 'docx',
            'six': 'pptx',
            'tool': 'docx',
            'opl': 'docx',
        }
        for proposal_type, extension in cases.items():
            self.assertEqual(app.export_access.proposal_export_format({'type': proposal_type}), extension)
        with self.assertRaisesRegex(ValueError, '固定使用 PPT'):
            app.export_docx.build([{'id': 'LY-1', 'type': 'six'}], app.UPLOAD_DIR)

    def test_ledger_endpoint_reuses_selection_and_scope_policy(self):
        status, _, _ = self.request('GET','/api/export/proposals.xlsx',headers={'Authorization':''})
        self.assertEqual(status,401)
        token = app.auth.login('56348',app.auth.DEFAULT_PASSWORD)['token']
        headers = {'Authorization':f'Bearer {token}','Content-Type':'application/json'}
        status, _, data = self.request('GET','/api/export/proposals/preview',headers=headers)
        selected = json.loads(data)['items']
        ids = [x['id'] for x in selected]
        status, _, workbook = self.request('POST','/api/export/proposals.xlsx',json.dumps({'ids':ids}).encode(),headers)
        self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(workbook)) as z:
            xml = '\n'.join(z.read(name).decode() for name in z.namelist() if name.startswith('xl/worksheets/sheet'))
            for item in selected:
                self.assertIn(item.get('sourceNo') or item['id'],xml)
        status, _, _ = self.request('POST','/api/export/proposals.xlsx',b'{"ids":["NOT-ALLOWED"]}',headers)
        self.assertEqual(status,403)
        status, _, _ = self.request('POST','/api/export/proposals.xlsx',b'{"ids":[]}',headers)
        self.assertEqual(status,400)

    def test_multi_department_submission_review_and_ledger_isolation(self):
        original, _, _ = app.get_states()
        departments = app.organization.departments(original) + [{'id':'qa-forging','name':'测试锻造厂','code':'QDZ','status':'启用'}]
        team = {'id':'QA-DZ-T','name':'热一组','section':'生产工段','departmentId':'qa-forging'}
        people = [
            {'id':'QA-DZ-E','workNo':'QA-DZ-E','name':'跨部门测试员工','role':'员工','level':'无','scope':'本人'},
            {'id':'QA-DZ-TL','workNo':'QA-DZ-TL','name':'跨部门测试班长','role':'班组长','level':'班组','scope':'热一组'},
            {'id':'QA-DZ-R','workNo':'QA-DZ-R','name':'跨部门测试评审','role':'员工','level':'评审员','scope':'测试锻造厂','departmentScopes':['qa-forging']},
        ]
        try:
            self.assertEqual(self.json_request('PUT','/api/state/heatstar_departments',{'value':departments})[0],200)
            app.set_state('heatstar_teams', original['heatstar_teams'] + [team])
            app.set_state('heatstar_people', original['heatstar_people'] + [{**p,'teamId':team['id'],'team':team['name'],'departmentId':'qa-forging','status':'在岗'} for p in people])
            actors = {p['workNo']:app.auth.login(p['workNo'],app.auth.DEFAULT_PASSWORD) for p in people}
            def call(actor, method, path, value=None):
                status, _, data = self.request(method,path,None if value is None else json.dumps(value).encode(),{'Authorization':'Bearer '+actor['token'],'Content-Type':'application/json'})
                return status, data
            employee = actors['QA-DZ-E']
            status, data = call(employee,'POST','/api/numbers/proposal?type=lean',{})
            self.assertEqual(status,201)
            number = json.loads(data)['number']
            self.assertTrue(number.startswith('JY-QDZ-'))
            item = {'id':number,'type':'lean','title':'跨部门测试方案','status':'pending_team','submitterId':'QA-DZ-E','firstAuthor':people[0]['name'],'teamId':team['id'],'team':team['name'],'departmentId':'qa-forging'}
            self.assertEqual(call(employee,'POST','/api/proposals/save',{'proposal':{**item,'departmentId':'heat-treatment'}})[0],400)
            status, data = call(employee,'POST','/api/proposals/save',{'proposal':item})
            self.assertEqual(status,200,data)
            item = json.loads(data)['proposal']
            original_team = app.auth.login('59628',app.auth.DEFAULT_PASSWORD)
            self.assertEqual(call(original_team,'POST','/api/proposals/save',{'proposal':{**item,'status':'pending_review','stars':2}})[0],403)
            status, data = call(actors['QA-DZ-TL'],'POST','/api/proposals/save',{'proposal':{**item,'status':'pending_review','stars':2}})
            self.assertEqual(status,200,data)
            item = json.loads(data)['proposal']
            self.assertEqual(call(app.auth.login('QA-R1',app.auth.DEFAULT_PASSWORD),'POST','/api/proposals/save',{'proposal':{**item,'status':'approved','stars':3}})[0],403)
            # An old whole-table snapshot cannot bypass the group review endpoint.
            self.json_request('PUT','/api/state/heatstar_proposals',{'value':[{**item,'status':'approved'}]})
            self.assertEqual(next(x for x in app.get_states()[0]['heatstar_proposals'] if x['id']==number)['status'],'pending_review')
            status, data = call(actors['QA-DZ-R'],'POST','/api/proposals/save',{'proposal':{**item,'status':'approved','stars':3,'points':999}})
            self.assertEqual(status,200,data)
            reviewed = json.loads(data)['proposal']
            self.assertEqual(reviewed['points'],0)
            self.assertEqual(reviewed['status'],'pending_company')
            status, _, result = self.json_request('POST','/api/proposals/save',{'proposal':{**reviewed,'status':'approved','stars':3}})
            self.assertEqual(status,200)
            self.assertEqual(result['proposal']['points'],5)
            status, data = call(employee,'GET','/api/export/proposals/preview')
            self.assertEqual([x['id'] for x in json.loads(data)['items']],[number])
            self.assertEqual(call(employee,'POST','/api/export/proposals.xlsx',{'ids':[number]})[0],200)
            self.assertEqual(call(original_team,'POST','/api/export/proposals.xlsx',{'ids':[number]})[0],403)
        finally:
            for key in ('heatstar_proposals','heatstar_people','heatstar_teams','heatstar_departments'):
                app.set_state(key, original[key])

    def test_parallel_number_reservation(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.json_request('POST', '/api/numbers/proposal?type=lean', {}), range(16)))
        self.assertTrue(all(result[0] == 201 for result in results))
        self.assertEqual(len({result[2]['number'] for result in results}), 16)

    def test_department_votes_are_atomic_and_recommendation_identity_is_server_owned(self):
        original = app.get_states()[0]['heatstar_proposals']
        try:
            employee=app.auth.login('56348',app.auth.DEFAULT_PASSWORD)['user']
            team=app.auth.login('59628',app.auth.DEFAULT_PASSWORD)['user']
            r1=app.auth.login('QA-R1',app.auth.DEFAULT_PASSWORD)['user']
            r2=app.auth.login('QA-R2',app.auth.DEFAULT_PASSWORD)['user']
            item=app.save_proposal_atomic(employee, {'id':'QA-CONCURRENT','type':'lean','title':'并发评分测试','status':'pending_team','submitterId':employee['workNo'],'firstAuthor':employee['name'],'team':employee['team']})
            item=app.save_proposal_atomic(team,{**item,'status':'pending_review','stars':3})
            def vote(user):
                try: return app.save_proposal_atomic(user,{**item,'status':'approved','stars':2})
                except app.proposal_rules.ProposalError as e: return e.status
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(vote,[r1,r1]))
            self.assertEqual(sum(x==409 for x in result),1)
            result=vote(r2)
            self.assertEqual(len(result['departmentReview']['votes']),2)
            self.assertEqual(result['departmentReview']['totalScore'],4)
            self.assertEqual(result['status'],'approved')
            self.assertEqual(result['points'],3)
        finally:
            app.set_state('heatstar_proposals',original)

    def test_proposal_group_review_and_resubmission(self):
        employee = app.auth.login('56348', app.auth.DEFAULT_PASSWORD)
        def save(user, proposal):
            payload = json.dumps({'proposal': proposal}).encode()
            status, _, data = self.request('POST', '/api/proposals/save', payload, {'Authorization': 'Bearer ' + user['token'], 'Content-Type': 'application/json'})
            return status, json.loads(data)
        user = employee['user']
        item = {'id': 'QA-FLOW', 'type': 'six', 'title': 'QA flow', 'status': 'pending_team', 'submitterId': user['workNo'], 'firstAuthor': user['name'], 'team': user['team'], 'section': user['section']}
        status, result = save(employee, item)
        self.assertEqual(status, 200)
        item = result['proposal']
        self.assertIn('serverUpdatedAt', item)
        status, _ = save(employee, {**item, 'status': 'approved'})
        self.assertEqual(status, 400)
        other_team = app.auth.login('59473', app.auth.DEFAULT_PASSWORD)
        status, _ = save(other_team, {**item, 'status': 'pending_review'})
        self.assertEqual(status, 403)
        team = app.auth.login('59628', app.auth.DEFAULT_PASSWORD)
        status, result = save(team, {**item, 'status': 'rejected', 'rejectReason': '补充改善证据'})
        self.assertEqual(status, 200)
        status, result = save(employee, {**result['proposal'], 'status': 'pending_team'})
        self.assertEqual(status, 200)
        item = result['proposal']
        for work_no, next_status in [('59628', 'pending_review'), ('QA-R1', 'approved')]:
            reviewer = app.auth.login(work_no, app.auth.DEFAULT_PASSWORD)
            status, result = save(reviewer, {**item, 'status': next_status, 'stars': 3, 'points': 5})
            self.assertEqual(status, 200, result)
            item = result['proposal']
        self.assertEqual(len(item['reviewHistory']), 4)
        self.assertEqual(item['status'], 'pending_review')
        duplicate = app.auth.login('QA-R1', app.auth.DEFAULT_PASSWORD)
        status, _ = save(duplicate, {**item, 'status':'approved','stars':1})
        self.assertEqual(status, 409)
        self.assertEqual(item['reviewHistory'][-1]['reviewerWorkNo'], 'QA-R1')
        second = app.auth.login('QA-R2', app.auth.DEFAULT_PASSWORD)
        status, result = save(second, {**item,'status':'approved','stars':1})
        self.assertEqual(status, 200)
        item = result['proposal']
        self.assertEqual(item['stars'], 2)
        self.assertEqual(item['departmentReview']['totalScore'], 4)
        status, _, bootstrap = self.json_request('GET', '/api/bootstrap')
        self.assertEqual(next(p for p in bootstrap['states']['heatstar_proposals'] if p['id'] == item['id'])['status'], 'approved')
        status, _, result = self.json_request('POST', '/api/proposals/delete', {'id': item['id']})
        self.assertEqual(status, 200)
        self.assertEqual(result['removed'], [item['id']])

    def test_tool_and_opl_follow_same_group_review(self):
        employee = app.auth.login('56348', app.auth.DEFAULT_PASSWORD)
        user = employee['user']

        def save(actor, proposal):
            payload = json.dumps({'proposal': proposal}).encode()
            status, _, data = self.request('POST', '/api/proposals/save', payload, {
                'Authorization': 'Bearer ' + actor['token'], 'Content-Type': 'application/json'
            })
            return status, json.loads(data)

        status, _ = save(employee, {
            'id': 'QA-UNKNOWN', 'type': 'unknown', 'title': 'invalid', 'status': 'pending_team',
            'submitterId': user['workNo'], 'firstAuthor': user['name'],
            'team': user['team'], 'section': user['section']
        })
        self.assertEqual(status, 400)

        reviewers = [('59628', 'pending_review'), ('QA-R1', 'approved'), ('QA-R2', 'approved')]
        for kind in ('tool', 'opl'):
            _, _, number = self.json_request('POST', f'/api/numbers/proposal?type={kind}', {})
            item = {
                'id': number['number'], 'sourceNo': number['number'], 'type': kind,
                'title': f'QA {kind}', 'status': 'pending_team',
                'submitterId': user['workNo'], 'firstAuthor': user['name'],
                'team': user['team'], 'section': user['section']
            }
            status, result = save(employee, item)
            self.assertEqual(status, 200, result)
            item = result['proposal']
            for work_no, next_status in reviewers:
                reviewer = app.auth.login(work_no, app.auth.DEFAULT_PASSWORD)
                status, result = save(reviewer, {**item, 'status': next_status, 'stars': 2, 'points': 3})
                self.assertEqual(status, 200, result)
                item = result['proposal']
            self.assertEqual(item['status'], 'approved')
            self.assertEqual([entry['level'] for entry in item['reviewHistory']], ['team', 'reviewer', 'reviewer'])
            status, _, result = self.json_request('POST', '/api/proposals/delete', {'id': item['id']})
            self.assertEqual(status, 200)

    def test_word_export_contains_tool_and_opl_templates(self):
        (app.UPLOAD_DIR / 'many.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 8 + b'\x00\x00\x00\x01\x00\x00\x00\x01')
        data = app.export_docx.build([
            {'id': 'GJ-1', 'type': 'tool', 'title': '移动工具车', 'toolCategory': '工装',
             'firstAuthor': '员工甲', 'team': '机动组', 'designIdea': '分类存放', 'applicationEffect': '减少寻找时间'},
            {'id': 'OPL-1', 'type': 'opl', 'title': '电偶准备流程', 'oplCategory': '工作技巧',
             'firstAuthor': '员工乙', 'team': '准备组',
             'lecturer': '员工乙', 'trainees': '准备组全员', 'trainingEvaluation': '能独立进行',
             'beforePhotos': ['/uploads/many.png'] * 8,
             'oplSteps': [
                 {'photo': '/uploads/many.png', 'text': f'操作说明 {index}'}
                 for index in range(1, 9)
             ]},
        ], app.UPLOAD_DIR)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read('word/document.xml').decode('utf-8')
        for expected in ('热处理厂工具开发', '移动工具车', '热处理厂 OPL 单点课程', '电偶准备流程', '操作说明 1', '步骤 8 图片', '能独立进行'):
            self.assertIn(expected, xml)
        self.assertEqual(xml.count('<w:drawing>'), 8)

    def test_structured_opl_requires_one_photo_and_text_per_step(self):
        user = app.auth.login('56348', app.auth.DEFAULT_PASSWORD)['user']
        base = {
            'id': 'OPL-PAIR-1', 'sourceNo': 'OPL-PAIR-1', 'type': 'opl',
            'title': '步骤配对测试', 'status': 'pending_team',
            'submitterId': user['workNo'], 'firstAuthor': user['name'],
            'team': user['team'], 'section': user['section']
        }
        with self.assertRaisesRegex(app.proposal_rules.ProposalError, '步骤 1 必须包含一张图片和一段说明'):
            app.proposal_rules.merge_one([], {**base, 'oplSteps': [{'photo': '/uploads/one.jpg', 'text': ''}]}, user)
        _, stored = app.proposal_rules.merge_one([], {
            **base,
            'oplSteps': [{'photo': '/uploads/one.jpg', 'text': '确认热电偶型号与图纸一致'}]
        }, user)
        self.assertEqual(stored['oplSteps'][0]['text'], '确认热电偶型号与图纸一致')

    def test_production_cors(self):
        for origin in ['https://gy.rechuangxing.xyz', 'https://gyht.rechuangxing.xyz']:
            status, headers, _ = self.request('OPTIONS', '/api/bootstrap', headers={'Origin': origin})
            self.assertEqual(status, 204)
            self.assertEqual(headers['Access-Control-Allow-Origin'], origin)


if __name__ == "__main__":
    unittest.main(verbosity=2)

