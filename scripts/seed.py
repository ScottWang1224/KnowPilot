"""Run explicitly: python -m scripts.seed. Insert-only fictional departments and content.

Existing content stays unchanged. Accounts come from registration or scripts.seed_admin.
"""
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from core.config import ROOT
from core.database import Database

DEPARTMENTS = [('MFG','製造部'),('PE','製程工程部'),('QA','品質保證部'),('RD','研發部'),('ADM','行政管理部')]
DOCUMENTS = [
('ADM-RULE-001','員工工作與資訊安全規範','工作紀律、公司資訊與帳號使用的共同規範。','規章',None,'company','1.0',
 '''一、適用範圍
本規範適用於智塗精密材料股份有限公司全體員工。

二、工作與資訊管理
1. 依部門規定完成出勤與工作交接。
2. 員工帳號限本人使用，不得分享密碼。
3. 公司文件應於授權系統內查閱，不得任意傳送至外部服務。
4. 發現異常登入或資料外洩疑慮時，立即通知行政管理部門。

三、文件使用
使用前確認文件版本。對內容有疑問時，向文件負責部門確認。

本文件為虛構企業展示資料。'''),
('ADM-TRN-001','新進員工教育訓練指南','認識公司、文件中心及部門協作方式。','教育訓練',None,'company','1.1',
 '''一、公司介紹
智塗精密材料專注於精密塗布與電子材料製造。本展示系統模擬企業內部知識入口。

二、到職第一週
1. 完成帳號啟用，確認部門與角色資料。
2. 閱讀公司規章與資訊安全規範。
3. 由部門主管安排製程、品質與安全訓練。
4. 練習查看公司公告及查找本部門 SOP。

三、工作中的知識查詢
先確認文件編號、版本與更新日期。無法存取文件時，向主管或文件負責人確認授權。

四、訓練紀錄
實際上線後應由負責部門保存訓練與考核紀錄。此階段不提供簽核功能。'''),
('QA-RULE-001','品質異常通報與批次追溯原則','全公司品質異常通報、識別及追溯的基本程序。','規章',None,'company','2.0',
 '''一、異常通報
發現外觀、尺寸、檢測紀錄或批次標示異常時，先依現場規範保留相關紀錄，通知主管與品質保證部。

二、批次識別
保存工單編號、原料批號、設備編號與作業時間，不得修改原始紀錄以掩蓋異常。

三、追溯與處置
由授權負責人依正式程序判定影響範圍，紀錄隔離、評估與後續處置。

四、結案
改善措施與驗證結果應可追溯至原通報事件。

此為系統展示資料，不替代真實工廠核准程序。'''),
('MFG-SOP-001','精密塗布設備開機與清潔 SOP','製造部開機確認、交接與清潔紀錄的標準範例。','SOP','MFG','department','1.2',
 '''一、目的與適用範圍
建立製造部設備作業的共同查核格式，適用於受訓且經授權之人員。

二、作業前確認
1. 查閱工單與適用的正式 SOP 版本。
2. 確認作業區、設備狀態與交接紀錄。
3. 發現異常時停止自行處置，依現場規範通知主管。

三、作業紀錄
依核准程序完成檢點並記錄設備編號、批次、時間與作業者。

四、清潔與交接
清潔方式與停機程序依設備核准文件執行。完成後保存查核與交接紀錄。

注意：本虛構範例不包含實際設備操作參數，不得用於真實設備作業。'''),
('MFG-TRN-001','製造部批次紀錄填寫訓練','工單、原料批號與製程紀錄的填寫原則。','教育訓練','MFG','department','1.0',
 '''一、訓練目標
正確識別工單、原料與設備，維持批次紀錄完整性。

二、基本欄位
工單編號、原料批號、設備編號、作業日期、作業者與交接者。

三、填寫原則
即時、清楚且可追溯。發現紀錄錯誤，依核准方式修正並留下修正原因。

四、訓練練習
使用虛構工單進行紀錄核對，再由部門主管確認學習結果。'''),
('RD-SOP-001','研發樣品管理與測試紀錄 SOP','研發部樣品編碼、測試紀錄與交接規則。','SOP','RD','department','1.0',
 '''一、樣品識別
以專案、批次及樣品序號建立識別碼，保存樣品來源與負責人。

二、測試紀錄
記錄測試目的、核准方法、儀器識別與原始結果，避免混用不同版本的方法。

三、樣品交接
交接時確認識別碼、數量與用途，由收件人確認紀錄。

四、資料保存
僅於授權系統內保存研發紀錄。涉及配方資訊的文件採更嚴格存取限制。'''),
('QA-SOP-001','檢驗紀錄審閱與留存 SOP','品質保證部檢驗紀錄完整性與文件留存的查核流程。','SOP','QA','department','1.3',
 '''一、審閱範圍
核對檢驗紀錄與批次識別、方法版本及原始資料的完整性。

二、查核項目
1. 批次與樣品編號是否一致。
2. 檢驗日期、儀器與人員是否完整。
3. 結果是否有對應的原始紀錄。
4. 異常是否依核准程序通報。

三、留存
依公司核准規範保存紀錄，不任意刪除或覆寫原始資料。'''),
('RD-SEC-001','研發配方資訊管理規範','研發部配方資訊的授權、交接與保存原則。','規章','RD','department','1.0',
 '''一、文件等級
本文件僅開放研發部人員查閱。

二、授權原則
實際配方與專案機密應依核准授權清單管理，保留必要的存取紀錄。

三、對外提供
須依公司核准程序辦理，不得自行分享配方、測試資料或內部文件。

四、交接與保管
確認接收人權限並保存交接紀錄。

本文件只有虛構管理規範，不包含任何真實配方。''')]


def seed(db):
    with db.transaction() as cursor:
        for code,name in DEPARTMENTS:
            cursor.execute('SELECT id FROM departments WHERE code=%s',(code,))
            if not cursor.fetchone():
                cursor.execute('INSERT INTO departments(code,name) VALUES(%s,%s)',(code,name))
        cursor.execute('SELECT id,code FROM departments')
        departments={r['code']:r['id'] for r in cursor.fetchall()}
        now=datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0)
        announcements=[
            ('welcome','KnowPilot 內部工作平台正式開放展示','公司通知',1,
             '歡迎使用 KnowPilot 企業內部工作平台。\n\n登入後可查看公司公告與授權文件。請確認自己的部門與角色資訊，並閱讀資訊安全規範。\n\n此平台為期初展示，所有公司、員工與文件均為虛構資料。'),
            ('training','新進員工教育訓練教材已更新','教育訓練',0,
             '新進員工教育訓練指南已更新至 1.1 版。\n\n請至文件中心查看最新教材，並與部門主管確認訓練安排。'),
            ('quality','品質異常通報與追溯規範提醒','品質通知',0,
             '請各部門確認批次識別與作業紀錄完整性。\n\n發現異常時，依核准程序通知主管及品質保證部，避免漏失原始紀錄。'),
            ('sop','製造部 SOP 與文件版本查核提醒','製造通知',0,
             '作業前請先確認適用 SOP 的文件編號與版本。\n\n本部門文件僅開放符合權限的員工查閱；如有疑問，請聯絡部門主管。')]
        for days,(key,title,category,pinned,content) in enumerate(announcements):
            cursor.execute('SELECT id FROM announcements WHERE seed_key=%s',(key,))
            if not cursor.fetchone():
                cursor.execute('''INSERT INTO announcements(seed_key,title,content,category,is_pinned,status,published_at)
                                  VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                               (key,title,content,category,pinned,'published',now-timedelta(days=days)))
        for code,title,summary,category,department,visibility,version,content in DOCUMENTS:
            if visibility=='department' and not department:
                raise ValueError('Department document requires department_id')
            cursor.execute('SELECT id FROM documents WHERE document_code=%s',(code,))
            if not cursor.fetchone():
                cursor.execute('''INSERT INTO documents(document_code,title,summary,content,category,department_id,visibility,version,status)
                                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                               (code,title,summary,content,category,departments.get(department),visibility,version,'published'))


if __name__=='__main__':
    load_dotenv(ROOT/'.env')
    seed(Database.from_env())
    print('Seed complete. Existing records were preserved.')
