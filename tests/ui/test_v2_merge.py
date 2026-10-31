from playwright.sync_api import expect,sync_playwright
from test_frontend_browser import site as site, chromium_path  # pytest fixture registration


def test_journey_manual_conflict_merge_validates_before_explicit_cas_save(site):
    url,pid,_,_=site
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=chromium_path(p),headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        try:
            page.goto(url+f'/#view=journeys&project={pid}')
            page.get_by_label('登录名',exact=True).fill('owner');page.get_by_label('密码',exact=True).fill('owner')
            page.get_by_role('button',name='打开工作空间').click()
            page.get_by_role('button',name='维护导览与结构',exact=True).click()
            page.get_by_role('button',name='新建流程',exact=True).click()
            title=page.get_by_label('流程名称',exact=True);title.fill('Merge base')
            page.get_by_role('button',name='保存流程',exact=True).click()
            expect(page.get_by_text('流程已保存为不可变修订',exact=True)).to_be_visible()
            w=page.request.get(url+f'/api/v1/projects/{pid}/workspace').json()['data'];aid=w['selected_analysis_set']['id']
            rows=page.request.get(url+f'/api/v2/projects/{pid}/journeys?analysis_set_id={aid}').json()['data']
            row=next(r for r in rows if r['dsl']['title']=='Merge base')
            csrf=page.request.get(url+'/api/v1/auth/session').json()['data']['csrf_token']
            title.fill('Local merged title')
            remote=page.request.post(url+f'/api/v2/journeys/{row["journey_id"]}/revisions',headers={'X-CSRF-Token':csrf,'Idempotency-Key':'merge-remote','If-Match':row['revision_id']},data={'analysis_set_id':aid,'dsl':dict(row['dsl'],title='Remote edited title')})
            assert remote.ok,remote.text()
            page.get_by_role('button',name='保存流程',exact=True).click()
            expect(title).to_have_value('Local merged title')
            page.get_by_role('button',name='比较并合并更新',exact=True).click()
            dialog=page.get_by_role('dialog')
            expect(dialog.get_by_role('heading',name='合并流程更新',exact=True)).to_be_visible()
            dialog.get_by_label('合并选择 title',exact=True).select_option('local')
            dialog.get_by_role('button',name='校验并应用为待保存草稿',exact=True).click()
            expect(dialog).to_have_count(0)
            expect(title).to_have_value('Local merged title')
            unchanged=page.request.get(url+f'/api/v2/projects/{pid}/journeys?analysis_set_id={aid}').json()['data']
            assert next(r for r in unchanged if r['journey_id']==row['journey_id'])['dsl']['title']=='Remote edited title'
            page.get_by_role('button',name='保存流程',exact=True).click()
            page.reload();page.get_by_role('button',name='维护导览与结构',exact=True).click();expect(title).to_have_value('Local merged title')
            page.get_by_role('button',name='自动排列',exact=True).click()
            page.get_by_role('button',name='撤销',exact=True).click()
        finally:browser.close()
