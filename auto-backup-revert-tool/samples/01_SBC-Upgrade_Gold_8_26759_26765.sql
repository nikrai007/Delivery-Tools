-- Block 1 — Field 3698 / KeyID 132
Delete FROM Level3Field WHERE OwnerID = 608 AND FieldId IN (3698);
Delete FROM MultiLevelField WHERE OwnerID = 608 AND FieldId IN (3698);
Delete FROM Level1Field WHERE OwnerID = 608 AND FieldId IN (3698);
Delete FROM LevelFieldDetails WHERE OwnerID = 608 AND FieldId IN (3698) AND KeyID = 132;
Delete FROM CustomfieldLookup WHERE OwnerID = 608 AND FieldId IN (3698) AND KeyID = 132;
Delete FROM ObjectSchema WHERE OwnerID = 608 AND FieldId IN (3698) AND KeyID = 132;
COMMIT;

-- Block 2 — Field 3699 / KeyID 43
Delete FROM Level3Field WHERE OwnerID = 608 AND FieldId IN (3699);
Delete FROM MultiLevelField WHERE OwnerID = 608 AND FieldId IN (3699);
Delete FROM Level1Field WHERE OwnerID = 608 AND FieldId IN (3699);
Delete FROM LevelFieldDetails WHERE OwnerID = 608 AND FieldId IN (3699) AND KeyID = 43;
Delete FROM CustomfieldLookup WHERE OwnerID = 608 AND FieldId IN (3699) AND KeyID = 43;
Delete FROM ObjectSchema WHERE OwnerID = 608 AND FieldId IN (3699) AND KeyID = 43;
COMMIT;

-- Block 3 — LookUp 27..37  (child LOOKUPEXTENDED → parent LookUpMaster)
Delete FROM LOOKUPEXTENDED where OwnerID = 608 and L1ParentId IN ( 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37);
Delete FROM LookUpMaster WHERE OwnerID = 608 AND LookUpId IN (27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37) AND GroupKey IN (21);
COMMIT;

-- Block 4 — CIS Job 6158
Delete FROM CIS_Job WHERE OwnerID = 608 AND JobID IN (6158);
Delete FROM CIS_Pass WHERE OwnerID = 608 AND JobID IN (6158);
Delete FROM CIS_Task WHERE OwnerID = 608 AND JobID IN (6158);
Delete FROM CIS_Mapping_Block WHERE OwnerID = 608 AND TaskID IN (SELECT TaskID FROM CIS_Task WHERE OwnerID = 608 AND JobID IN (6158) );
Delete FROM CIS_TaskExecutable WHERE OwnerID = 608 AND TaskID IN (SELECT TaskID FROM CIS_Task WHERE OwnerID = 608 AND JobID IN (6158) );
Delete From CIS_ScheduledJob where OwnerID = 608 AND JobID IN (6158);
Delete From CIS_Schedule where OwnerID = 608 AND ScheduleID IN (Select ScheduleID From CIS_ScheduledJob where OwnerID = 608 AND JobID IN (6158));
COMMIT;

-- Block 5 — CIS Job 6157
Delete FROM CIS_Job WHERE OwnerID = 608 AND JobID IN (6157);
Delete FROM CIS_Pass WHERE OwnerID = 608 AND JobID IN (6157);
Delete FROM CIS_Task WHERE OwnerID = 608 AND JobID IN (6157);
Delete FROM CIS_Mapping_Block WHERE OwnerID = 608 AND TaskID IN (SELECT TaskID FROM CIS_Task WHERE OwnerID = 608 AND JobID IN (6157) );
Delete FROM CIS_TaskExecutable WHERE OwnerID = 608 AND TaskID IN (SELECT TaskID FROM CIS_Task WHERE OwnerID = 608 AND JobID IN (6157) );
Delete From CIS_ScheduledJob where OwnerID = 608 AND JobID IN (6157);
Delete From CIS_Schedule where OwnerID = 608 AND ScheduleID IN (Select ScheduleID From CIS_ScheduledJob where OwnerID = 608 AND JobID IN (6157));
COMMIT;

-- Block 6 — CIS Task 6889,6905
Delete FROM CIS_Task WHERE OwnerID = 608 AND TaskID IN (6889,6905);
Delete FROM CIS_Mapping_Block WHERE OwnerID = 608 AND TaskID IN (6889,6905);
Delete FROM CIS_TaskExecutable WHERE OwnerID = 608 AND TaskID IN (6889,6905);
Delete From CIS_ScheduledJob where OwnerID = 608 AND JobID IN (5020);
Delete From CIS_Schedule where OwnerID = 608 AND ScheduleID IN (Select ScheduleID From CIS_ScheduledJob where OwnerID = 608 AND JobID IN (5020));
COMMIT;

-- Block 7 — Offer purge (single statement)
DELETE FROM off_ex1 e WHERE e.off_ex1_id IN ( SELECT OFFERID FROM SBC_PURGEOFFER_TEMP_LT );
COMMIT;

-- Block 8 — Offers (single statement)
DELETE FROM offers o WHERE o.OFFERID IN ( SELECT OFFERID FROM SBC_PURGEOFFER_TEMP_LT );

-- Audit/log/lookup that must be skipped
DELETE FROM SBC_OFFERS_LT_LOG WHERE 1 = 1;
DELETE FROM SBC_OFFERS_PREVALID_LT_LOG WHERE 1 = 1;
DELETE FROM SBC_DISPOSE_OFFERS_LT_lOG WHERE 1 = 1;
DELETE FROM SBC_DISPOSE_OFFERS_TEMP_LT_LOG WHERE 1 = 1;
COMMIT;
