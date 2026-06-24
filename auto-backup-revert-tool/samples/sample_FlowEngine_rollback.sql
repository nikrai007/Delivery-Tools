-- ============================================================================
-- Sample migration bundle to exercise AutoBackupRevert
-- ----------------------------------------------------------------------------
-- This single file contains TWO independent DELETE blocks separated by a
-- COMMIT, plus a few audit/log/lookup tables that must be skipped from the
-- generated BACKUP / REVERT scripts.
--
-- Upload this file directly on the "New Job" page (no need to zip).
--
-- After processing, REVERT.sql should:
--   1.  Restore Block 1 tables in REVERSE delete order
--       (FlowVersions → FlowDefinition → … → ItemVisibility).
--   2.  Restore Block 2 tables in REVERSE delete order
--       (FlowConfigMap → FlowAuditQueue).
--   3.  Skip USER_LOG, FLOW_AUDIT_LOG, REGION_LT, REF_STATUS_LT,
--       SBC_AUDIT_HISTORY, and SBC_RUN_TRACE entirely — neither backed up
--       nor restored — and call them out in an "OMITTED" banner at the top.
-- ============================================================================


-- ---- Block 1: full Flow tree for OwnerId 608, Flow 5197, version 1.0 -------
-- Order below is CHILD → PARENT (correct for the migration DELETE chain).
-- Reversal in REVERT must flip it to PARENT → CHILD.

DELETE FROM ItemVisibility    WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM SwitchFlowMapping WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM TransitionActor   WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM Transition        WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM FlowTransition    WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';

-- Audit & lookup rows below should be SKIPPED by the tool (no backup, no revert).
DELETE FROM USER_LOG          WHERE FlowId IN (5197) AND CreatedOn >= DATE '2026-06-01';
DELETE FROM FLOW_AUDIT_LOG    WHERE FlowId IN (5197);
DELETE FROM REGION_LT         WHERE RegionCode = 'APAC-SG';
DELETE FROM REF_STATUS_LT     WHERE StatusCode IN ('STG', 'ACT');
DELETE FROM SBC_AUDIT_HISTORY WHERE EntityId  = 5197;
DELETE FROM SBC_RUN_TRACE     WHERE RunDate >= DATE '2026-06-01';

DELETE FROM FlowStepField     WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM FlowSteps         WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM FlowStage         WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM FlowActor         WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM FlowLane          WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM FlowDefinition    WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';
DELETE FROM FlowVersions      WHERE OwnerId = 608 AND FlowId IN (5197) AND Version = '1.0';

COMMIT;
-- ^ COMMIT is a non-DELETE statement, so it closes Block 1 and opens Block 2.


-- ---- Block 2: rollback of a separate FlowConfig change ---------------------
DELETE FROM FlowAuditQueue WHERE BatchId = 'BX-2026-Q2-77';
DELETE FROM FlowConfigMap  WHERE BatchId = 'BX-2026-Q2-77';

COMMIT;
