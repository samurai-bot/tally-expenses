-- Seed data — SAMPLE / DUMMY data for demos and local testing.
-- Not real financial data. Re-runnable (ON CONFLICT DO NOTHING). Run AFTER schema.sql.

-- ── Accounts ───────────────────────────────────────────────────────────────
INSERT INTO accounts (account_id, name, currency, type, active, sort_order) VALUES
 ('DBS','Bank A','SGD','asset',true,1),
 ('OCBC','Bank B','SGD','asset',true,2),
 ('MBB_ISAAVY','Savings account','SGD','asset',true,3),
 ('WISE','Multi-currency card','SGD','asset',true,4),
 ('CASH_SGD','Cash on hand','SGD','asset',true,6),
 ('DBS_CC','Credit Card A','SGD','liability',true,7),
 ('MBB_CC','Credit Card B','SGD','liability',true,8),
 ('MBB_MYR','Bank C (MYR)','MYR','asset',true,9),
 ('TUNAI','Cash MYR','MYR','asset',true,10),
 ('TNGO','E-wallet (MYR)','MYR','asset',true,11)
ON CONFLICT (account_id) DO NOTHING;

-- ── Categories (is_discretionary drives the burn-rate calc) ─────────────────
INSERT INTO categories (name, is_discretionary, sort_order) VALUES
 ('Allowance',false,1),
 ('Housing',false,2),
 ('Insurance',false,3),
 ('Telco',false,4),
 ('Transport',true,5),
 ('Subscriptions',false,6),
 ('Tax',false,7),
 ('Food',true,8),
 ('Family',false,9),
 ('Other',true,10),
 ('Transfer',false,12)
ON CONFLICT (name) DO NOTHING;

-- ── Recurring templates (sample values) ────────────────────────────────────
INSERT INTO recurring (recur_id,name,account_id,flow,category,amount,currency,day_of_month,active,start_date,end_date,note) VALUES
 ('R01','Monthly allowance','DBS','expense','Allowance',1500.00,'SGD',25,true,'2026-01-01',NULL,''),
 ('R02','Mortgage','OCBC','expense','Housing',2000.00,'SGD',2,true,'2026-01-01',NULL,''),
 ('R03','Life insurance','DBS','expense','Insurance',300.00,'SGD',18,true,'2026-01-01',NULL,''),
 ('R04','Health insurance','DBS_CC','expense','Insurance',120.00,'SGD',13,true,'2026-01-01',NULL,''),
 ('R05','Mobile plan A','MBB_CC','expense','Telco',50.00,'SGD',14,true,'2026-01-01',NULL,''),
 ('R06','Mobile plan B','MBB_CC','expense','Telco',45.00,'SGD',25,true,'2026-01-01',NULL,''),
 ('R07','Cloud storage','MBB_CC','expense','Subscriptions',4.00,'SGD',3,true,'2026-01-01',NULL,''),
 ('R08','AI subscription','DBS_CC','expense','Subscriptions',150.00,'SGD',30,true,'2026-01-01',NULL,''),
 ('R09','Streaming service','MBB_CC','expense','Subscriptions',30.00,'SGD',2,true,'2026-01-01',NULL,''),
 ('R10','Music family plan','WISE','expense','Subscriptions',28.00,'SGD',23,true,'2026-01-01',NULL,''),
 ('R11','Tax instalment','DBS','expense','Tax',800.00,'SGD',6,true,'2026-01-01','2027-05-31',''),
 ('R12','Parent support','MBB_MYR','expense','Family',1000.00,'MYR',1,true,'2026-01-01',NULL,''),
 ('R13','Variable bill','MBB_MYR','expense','Telco',0.00,'MYR',1,false,'2026-01-01',NULL,'set amt + activate'),
 ('R14','Parent support 2','MBB_MYR','expense','Family',0.00,'MYR',1,false,'2026-01-01',NULL,'set amt + activate')
ON CONFLICT (recur_id) DO NOTHING;

-- ── Balances (stocks) — sample history 31 May → 3 Jun ──────────────────────
INSERT INTO balances (snap_date, account_id, balance, currency, source, note) VALUES
 ('2026-05-31','DBS',8500.00,'SGD','seed','month-end'),
 ('2026-05-31','OCBC',3000.00,'SGD','seed','month-end'),
 ('2026-05-31','MBB_ISAAVY',500.00,'SGD','seed','month-end'),
 ('2026-05-31','WISE',120.00,'SGD','seed','month-end'),
 ('2026-05-31','CASH_SGD',60.00,'SGD','seed','month-end'),
 ('2026-05-31','DBS_CC',0,'SGD','seed','month-end'),
 ('2026-05-31','MBB_CC',0,'SGD','seed','month-end'),
 ('2026-05-31','MBB_MYR',5200.00,'MYR','seed','month-end'),
 ('2026-05-31','TUNAI',100.00,'MYR','seed','month-end'),
 ('2026-05-31','TNGO',50.00,'MYR','seed','month-end'),
 ('2026-06-01','DBS',8500.00,'SGD','seed','carry-forward opening'),
 ('2026-06-01','OCBC',3000.00,'SGD','seed','carry-forward opening'),
 ('2026-06-01','MBB_ISAAVY',500.00,'SGD','seed','carry-forward opening'),
 ('2026-06-01','WISE',120.00,'SGD','seed','carry-forward opening'),
 ('2026-06-01','CASH_SGD',60.00,'SGD','seed','carry-forward opening'),
 ('2026-06-01','DBS_CC',0,'SGD','seed','carry-forward opening'),
 ('2026-06-01','MBB_CC',0,'SGD','seed','carry-forward opening'),
 ('2026-06-01','MBB_MYR',5200.00,'MYR','seed','carry-forward opening'),
 ('2026-06-01','TUNAI',100.00,'MYR','seed','carry-forward opening'),
 ('2026-06-01','TNGO',50.00,'MYR','seed','carry-forward opening'),
 ('2026-06-02','DBS',8200.00,'SGD','form',''),
 ('2026-06-02','OCBC',3000.00,'SGD','form',''),
 ('2026-06-02','MBB_ISAAVY',500.00,'SGD','form',''),
 ('2026-06-02','WISE',120.00,'SGD','form',''),
 ('2026-06-02','CASH_SGD',55.00,'SGD','form',''),
 ('2026-06-02','DBS_CC',100.00,'SGD','form',''),
 ('2026-06-02','MBB_CC',120.00,'SGD','form',''),
 ('2026-06-02','MBB_MYR',5000.00,'MYR','form',''),
 ('2026-06-02','TUNAI',100.00,'MYR','form',''),
 ('2026-06-02','TNGO',50.00,'MYR','form',''),
 ('2026-06-03','DBS',8000.00,'SGD','form',''),
 ('2026-06-03','OCBC',3000.00,'SGD','form',''),
 ('2026-06-03','MBB_ISAAVY',500.00,'SGD','form',''),
 ('2026-06-03','WISE',120.00,'SGD','form',''),
 ('2026-06-03','CASH_SGD',50.00,'SGD','form',''),
 ('2026-06-03','DBS_CC',200.00,'SGD','form',''),
 ('2026-06-03','MBB_CC',150.00,'SGD','form',''),
 ('2026-06-03','MBB_MYR',5000.00,'MYR','form',''),
 ('2026-06-03','TUNAI',100.00,'MYR','form',''),
 ('2026-06-03','TNGO',50.00,'MYR','form','')
ON CONFLICT (snap_date, account_id) DO NOTHING;

-- ── Transactions (flows) — sample ledger ───────────────────────────────────
INSERT INTO transactions (txn_id, txn_date, account_id, flow, category, amount, currency, source, note) VALUES
 ('T0002','2026-06-02','OCBC','expense','Housing',2000.00,'SGD','recurring:R02','auto'),
 ('T0003','2026-06-01','MBB_CC','expense','Subscriptions',30.00,'SGD','recurring:R09','auto'),
 ('T0004','2026-06-02','CASH_SGD','expense','Food',12.50,'SGD','manual','lunch'),
 ('T0005','2026-06-03','MBB_CC','expense','Subscriptions',4.00,'SGD','recurring:R07','auto'),
 ('T20260602083000','2026-06-02','MBB_CC','expense','Food',8.00,'SGD','manual','coffee'),
 ('T20260602133000','2026-06-02','DBS','expense','Other',100.00,'SGD','manual','misc'),
 ('T20260602140000','2026-06-02','MBB_CC','expense','Transport',15.00,'SGD','manual','taxi'),
 ('T20260603090000','2026-06-03','MBB_CC','expense','Transport',20.00,'SGD','manual','ride to office'),
 ('T20260603130000','2026-06-03','DBS','expense','Food',6.00,'SGD','manual','lunch'),
 ('T20260603190000','2026-06-03','MBB_CC','expense','Food',10.00,'SGD','manual','dinner'),
 ('T20260604120000','2026-06-04','DBS','expense','Food',7.00,'SGD','manual','lunch'),
 ('T20260604190000','2026-06-04','MBB_CC','expense','Food',25.00,'SGD','manual','groceries')
ON CONFLICT (txn_id) DO NOTHING;
