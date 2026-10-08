export type FrontendConfig = {
	backend_command: string[];
	initial_prompt?: string | null;
};

export type TranscriptItem = {
	role: 'system' | 'user' | 'assistant' | 'tool' | 'tool_result' | 'log' | 'status';
	text: string;
	tool_name?: string;
	tool_input?: Record<string, unknown>;
	is_error?: boolean;
};

export type ImageAttachmentPayload = {
	media_type: string;
	data: string;
	source_path?: string;
};

export type TaskSnapshot = {
	id: string;
	type: string;
	status: string;
	description: string;
	metadata: Record<string, string>;
};

export type McpServerSnapshot = {
	name: string;
	state: string;
	detail?: string;
	transport?: string;
	auth_configured?: boolean;
	tool_count?: number;
	resource_count?: number;
};

export type SelectOptionPayload = {
	value: string;
	label: string;
	description?: string;
	active?: boolean;
};

export type TodoItemSnapshot = {
	text: string;
	checked: boolean;
};

export type BackendEvent = {
	type: string;
	message?: string | null;
	item?: TranscriptItem | null;
	state?: Record<string, unknown> | null;
	tasks?: TaskSnapshot[] | null;
	mcp_servers?: McpServerSnapshot[] | null;
	commands?: string[] | null;
	modal?: Record<string, unknown> | null;
	select_options?: SelectOptionPayload[] | null;
	tool_name?: string | null;
	output?: string | null;
	is_error?: boolean | null;
	compact_phase?: string | null;
	compact_trigger?: string | null;
	attempt?: number | null;
	compact_checkpoint?: string | null;
	compact_metadata?: Record<string, unknown> | null;
	// New event payloads
	todo_items?: TodoItemSnapshot[] | null;
	todo_markdown?: string | null;
	plan_mode?: string | null;
};
