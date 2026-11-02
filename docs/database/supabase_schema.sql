
CREATE TABLE projects (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	slug VARCHAR(80) NOT NULL, 
	owner_actor_id VARCHAR(120) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_projects_status CHECK (status IN ('active', 'archived')), 
	CONSTRAINT ck_projects_version CHECK (version > 0), 
	UNIQUE (slug)
)

;


CREATE TABLE repository_connections (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	provider VARCHAR(24) NOT NULL, 
	provider_repository_id INTEGER NOT NULL, 
	owner VARCHAR(100) NOT NULL, 
	repository VARCHAR(100) NOT NULL, 
	base_branch VARCHAR(250) NOT NULL, 
	installation_id VARCHAR(32) NOT NULL, 
	permission_status VARCHAR(32) NOT NULL, 
	verified_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_repo_connection_project_repo UNIQUE (project_id, provider, provider_repository_id), 
	CONSTRAINT ck_repo_connection_provider_id CHECK (provider_repository_id > 0), 
	CONSTRAINT ck_repo_connection_version CHECK (version > 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
)

;


CREATE TABLE target_environments (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	base_url VARCHAR(2048) NOT NULL, 
	environment_kind VARCHAR(24) NOT NULL, 
	authorization_status VARCHAR(24) NOT NULL, 
	allowed_hosts JSON NOT NULL, 
	version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_target_environment_kind CHECK (environment_kind IN ('demo', 'development', 'staging', 'other')), 
	CONSTRAINT ck_target_environment_authorization CHECK (authorization_status IN ('pending', 'approved', 'revoked')), 
	CONSTRAINT ck_target_environment_version CHECK (version > 0), 
	CONSTRAINT uq_target_environment_project_name UNIQUE (project_id, name), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
)

;


CREATE TABLE test_flows (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	description TEXT, 
	status VARCHAR(24) NOT NULL, 
	version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_test_flow_status CHECK (status IN ('active', 'archived')), 
	CONSTRAINT ck_test_flow_version CHECK (version > 0), 
	CONSTRAINT uq_test_flow_project_name UNIQUE (project_id, name), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
)

;


CREATE TABLE target_authorization_events (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	target_environment_id VARCHAR(36) NOT NULL, 
	action VARCHAR(24) NOT NULL, 
	actor_id VARCHAR(120) NOT NULL, 
	target_url_snapshot VARCHAR(2048) NOT NULL, 
	allowed_hosts_snapshot JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_target_authorization_event_action CHECK (action IN ('authorized', 'revoked')), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(target_environment_id) REFERENCES target_environments (id) ON DELETE RESTRICT
)

;


CREATE TABLE test_flow_versions (
	id VARCHAR(36) NOT NULL, 
	test_flow_id VARCHAR(36) NOT NULL, 
	version_number INTEGER NOT NULL, 
	schema_version INTEGER NOT NULL, 
	definition_json JSON NOT NULL, 
	definition_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(120), 
	PRIMARY KEY (id), 
	CONSTRAINT ck_test_flow_version_number CHECK (version_number > 0), 
	CONSTRAINT ck_test_flow_schema_version CHECK (schema_version > 0), 
	CONSTRAINT ck_test_flow_version_status CHECK (status IN ('draft', 'published', 'retired')), 
	CONSTRAINT uq_test_flow_version_number UNIQUE (test_flow_id, version_number), 
	FOREIGN KEY(test_flow_id) REFERENCES test_flows (id) ON DELETE RESTRICT
)

;


CREATE TABLE validation_reports (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	repository_connection_id VARCHAR(36) NOT NULL, 
	proposal_id VARCHAR(64) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	base_revision VARCHAR(64) NOT NULL, 
	diff_hash VARCHAR(64) NOT NULL, 
	policy_version VARCHAR(80) NOT NULL, 
	checks JSON NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(120) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_validation_reports_status CHECK (status IN ('verified', 'failed')), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(repository_connection_id) REFERENCES repository_connections (id) ON DELETE RESTRICT
)

;


CREATE TABLE github_operations (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	repository_connection_id VARCHAR(36) NOT NULL, 
	validation_report_id VARCHAR(36) NOT NULL, 
	proposal_id VARCHAR(64) NOT NULL, 
	actor_id VARCHAR(120) NOT NULL, 
	operation_type VARCHAR(40) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	idempotency_key VARCHAR(128) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	expected_base_sha VARCHAR(64) NOT NULL, 
	diff_hash VARCHAR(64) NOT NULL, 
	branch_name VARCHAR(250), 
	commit_sha VARCHAR(64), 
	pull_request_number INTEGER, 
	pull_request_url TEXT, 
	draft BOOLEAN, 
	attempt_count INTEGER NOT NULL, 
	safe_error_code VARCHAR(80), 
	safe_error_message TEXT, 
	last_reconciled_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_github_operation_project_idempotency UNIQUE (project_id, idempotency_key), 
	CONSTRAINT ck_github_operations_type CHECK (operation_type IN ('check_permissions', 'create_branch', 'create_commit', 'create_pull_request', 'reconcile_remote_state')), 
	CONSTRAINT ck_github_operations_status CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'remote_outcome_unknown', 'canceled')), 
	CONSTRAINT ck_github_operations_attempts CHECK (attempt_count >= 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(repository_connection_id) REFERENCES repository_connections (id) ON DELETE RESTRICT, 
	FOREIGN KEY(validation_report_id) REFERENCES validation_reports (id) ON DELETE RESTRICT
)

;


CREATE TABLE qa_runs (
	id VARCHAR(64) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	target_environment_id VARCHAR(36) NOT NULL, 
	test_flow_id VARCHAR(36) NOT NULL, 
	test_flow_version_id VARCHAR(36) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	outcome VARCHAR(32) NOT NULL, 
	current_stage VARCHAR(40) NOT NULL, 
	trigger_source VARCHAR(24) NOT NULL, 
	requested_by VARCHAR(120), 
	viewport_matrix_json JSON NOT NULL, 
	target_revision VARCHAR(128), 
	repository_base_revision VARCHAR(128), 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	cancellation_requested_at TIMESTAMP WITH TIME ZONE, 
	failure_code VARCHAR(80), 
	failure_message_safe TEXT, 
	correlation_id VARCHAR(64) NOT NULL, 
	version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_qa_run_status CHECK (status IN ('created','queued','running','completed','failed','canceled','timed_out','partial')), 
	CONSTRAINT ck_qa_run_outcome CHECK (outcome IN ('passed','issues_found','test_failed','analysis_inconclusive','analysis_unavailable','mixed','not_applicable')), 
	CONSTRAINT ck_qa_run_trigger_source CHECK (trigger_source IN ('manual','github_webhook','scheduled')), 
	CONSTRAINT ck_qa_run_version CHECK (version > 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(target_environment_id) REFERENCES target_environments (id) ON DELETE RESTRICT, 
	FOREIGN KEY(test_flow_id) REFERENCES test_flows (id) ON DELETE RESTRICT, 
	FOREIGN KEY(test_flow_version_id) REFERENCES test_flow_versions (id) ON DELETE RESTRICT
)

;


CREATE TABLE artifacts (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(64) NOT NULL, 
	storage_backend VARCHAR(24) NOT NULL, 
	object_key VARCHAR(1000) NOT NULL, 
	content_type VARCHAR(100) NOT NULL, 
	byte_size INTEGER NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_artifacts_storage_backend CHECK (storage_backend IN ('local')), 
	CONSTRAINT ck_artifacts_byte_size CHECK (byte_size > 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(run_id) REFERENCES qa_runs (id) ON DELETE RESTRICT
)

;


CREATE TABLE detection_attempts (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	adapter_label VARCHAR(100) NOT NULL, 
	is_mock BOOLEAN NOT NULL, 
	model_label VARCHAR(200), 
	prompt_version VARCHAR(100) NOT NULL, 
	schema_version INTEGER NOT NULL, 
	attempts INTEGER NOT NULL, 
	duration_ms FLOAT NOT NULL, 
	outcome VARCHAR(32), 
	reason_code VARCHAR(64), 
	reason_message VARCHAR(500), 
	findings_count INTEGER NOT NULL, 
	warnings JSON NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_detection_attempts_count CHECK (attempts > 0), 
	CONSTRAINT ck_detection_attempts_findings CHECK (findings_count >= 0), 
	CONSTRAINT ck_detection_attempts_duration CHECK (duration_ms >= 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(run_id) REFERENCES qa_runs (id) ON DELETE RESTRICT
)

;


CREATE TABLE patch_attempts (
	id VARCHAR(80) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	finding_id VARCHAR(64) NOT NULL, 
	run_id VARCHAR(64), 
	repository_connection_id VARCHAR(36), 
	status VARCHAR(40) NOT NULL, 
	attempt_number INTEGER NOT NULL, 
	base_revision VARCHAR(128), 
	branch_name VARCHAR(250), 
	model_provider VARCHAR(80) NOT NULL, 
	model_id VARCHAR(160), 
	prompt_version VARCHAR(80), 
	patch_format_version INTEGER NOT NULL, 
	changed_files_json JSON NOT NULL, 
	summary TEXT NOT NULL, 
	diff_hash VARCHAR(64) NOT NULL, 
	policy_check_result JSON NOT NULL, 
	validated_patch_json JSON, 
	proposal_record_json JSON NOT NULL, 
	validation_report_id VARCHAR(36), 
	requested_by VARCHAR(120), 
	failure_code VARCHAR(80), 
	failure_message_safe TEXT, 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_patch_attempt_status CHECK (status IN ('requested','queued','generating','proposed','rejected_by_policy','applying','validating','validation_passed','validation_failed','validation_inconclusive','blocked','pr_eligible','pr_requested','pr_created','pr_failed','remote_outcome_unknown','canceled')), 
	CONSTRAINT ck_patch_attempt_number CHECK (attempt_number > 0), 
	CONSTRAINT ck_patch_format_version CHECK (patch_format_version > 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(run_id) REFERENCES qa_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(repository_connection_id) REFERENCES repository_connections (id) ON DELETE RESTRICT
)

;


CREATE TABLE pull_requests (
	id VARCHAR(36) NOT NULL, 
	github_operation_id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	repository_connection_id VARCHAR(36) NOT NULL, 
	proposal_id VARCHAR(64) NOT NULL, 
	provider_pr_number INTEGER NOT NULL, 
	url TEXT NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	branch_name VARCHAR(250) NOT NULL, 
	base_branch VARCHAR(250) NOT NULL, 
	base_sha VARCHAR(64) NOT NULL, 
	head_sha VARCHAR(64) NOT NULL, 
	title VARCHAR(250) NOT NULL, 
	body_template_version VARCHAR(40) NOT NULL, 
	last_synced_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_pull_request_remote UNIQUE (repository_connection_id, provider_pr_number), 
	CONSTRAINT ck_pull_requests_status CHECK (status IN ('open', 'draft', 'closed', 'merged', 'unknown')), 
	CONSTRAINT ck_pull_requests_number CHECK (provider_pr_number > 0), 
	UNIQUE (github_operation_id), 
	FOREIGN KEY(github_operation_id) REFERENCES github_operations (id) ON DELETE RESTRICT, 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(repository_connection_id) REFERENCES repository_connections (id) ON DELETE RESTRICT
)

;


CREATE TABLE run_steps (
	id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(64) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	step_key VARCHAR(100), 
	step_type VARCHAR(40) NOT NULL, 
	display_label VARCHAR(160) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	outcome VARCHAR(32), 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	duration_ms INTEGER, 
	failure_code VARCHAR(80), 
	failure_message_safe TEXT, 
	input_summary_json JSON NOT NULL, 
	result_summary_json JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_run_step_ordinal CHECK (ordinal > 0), 
	CONSTRAINT ck_run_step_status CHECK (status IN ('queued','running','passed','failed','skipped','timed_out','canceled','unavailable','inconclusive','blocked')), 
	CONSTRAINT uq_run_step_ordinal UNIQUE (run_id, ordinal), 
	FOREIGN KEY(run_id) REFERENCES qa_runs (id) ON DELETE RESTRICT
)

;


CREATE TABLE workflow_events (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(64), 
	github_operation_id VARCHAR(36), 
	event_type VARCHAR(80) NOT NULL, 
	event_version INTEGER NOT NULL, 
	actor_type VARCHAR(32) NOT NULL, 
	actor_id VARCHAR(120) NOT NULL, 
	correlation_id VARCHAR(64) NOT NULL, 
	payload JSON NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_workflow_events_version CHECK (event_version > 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_workflow_events_run_id_qa_runs FOREIGN KEY(run_id) REFERENCES qa_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(github_operation_id) REFERENCES github_operations (id) ON DELETE RESTRICT
)

;


CREATE TABLE evidence (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(64) NOT NULL, 
	artifact_id VARCHAR(36) NOT NULL, 
	observation_json JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(run_id) REFERENCES qa_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(artifact_id) REFERENCES artifacts (id) ON DELETE RESTRICT
)

;


CREATE TABLE findings (
	id VARCHAR(64) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(64) NOT NULL, 
	detection_attempt_id VARCHAR(36), 
	category VARCHAR(48) NOT NULL, 
	severity VARCHAR(24), 
	confidence FLOAT, 
	status VARCHAR(24) NOT NULL, 
	is_current BOOLEAN NOT NULL, 
	record_json JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_findings_status CHECK (status IN ('detected','patch_proposed')), 
	CONSTRAINT ck_findings_version CHECK (version > 0), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	FOREIGN KEY(run_id) REFERENCES qa_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(detection_attempt_id) REFERENCES detection_attempts (id) ON DELETE RESTRICT
)

;

CREATE INDEX ix_repo_connections_project_updated ON repository_connections (project_id, updated_at);
CREATE INDEX ix_target_environments_project ON target_environments (project_id, created_at);
CREATE INDEX ix_target_authorization_events_target ON target_authorization_events (target_environment_id, created_at);
CREATE INDEX ix_validation_reports_proposal_created ON validation_reports (proposal_id, created_at);
CREATE INDEX ix_github_operations_project_state ON github_operations (project_id, status, created_at);
CREATE INDEX ix_github_operations_proposal ON github_operations (proposal_id, created_at);
CREATE INDEX ix_qa_runs_project_status_created ON qa_runs (project_id, status, created_at);
CREATE INDEX ix_artifacts_run_created ON artifacts (run_id, created_at);
CREATE INDEX ix_detection_attempts_run_created ON detection_attempts (run_id, created_at, id);
CREATE INDEX ix_patch_attempts_project_created ON patch_attempts (project_id, created_at);
CREATE INDEX ix_patch_attempts_run ON patch_attempts (run_id, created_at);
CREATE INDEX ix_patch_attempts_finding ON patch_attempts (finding_id, created_at);
CREATE INDEX ix_pull_requests_project_updated ON pull_requests (project_id, updated_at);
CREATE INDEX ix_run_steps_run ON run_steps (run_id, ordinal);
CREATE INDEX ix_workflow_events_run_occurred ON workflow_events (run_id, occurred_at);
CREATE INDEX ix_workflow_events_project_occurred ON workflow_events (project_id, occurred_at);
CREATE INDEX ix_evidence_run_created ON evidence (run_id, created_at, id);
CREATE INDEX ix_findings_project_status_created ON findings (project_id, status, created_at);
CREATE INDEX ix_findings_run_current_created ON findings (run_id, is_current, created_at);
