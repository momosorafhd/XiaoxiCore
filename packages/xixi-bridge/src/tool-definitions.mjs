// Pure tool contracts shared with the existing DSH host plugin. No model/runtime imports.
const str = (description) => ({ type: "string", required: true, description });
export const definitions = {
  library_record: {description:"Save important content via the unified Library writer. Materials require an explicit summary; other categories do not.",parameters:{category:str("One of 事实与经历/认识与判断/兴趣/黑话与名词解释/资料"),body:str("Full content"),title:{type:"string"},summary:{type:"string"},tags:{type:"array",items:{type:"string"}},aliases:{type:"array",items:{type:"string"}},needsReview:{type:"boolean"},sourceRefs:{type:"array",items:{type:"string"}},requestId:{type:"string"}}},
  library_revise: {description:"Revise an item using its exact version, keeping keyword and vector indexes in sync.",parameters:{itemId:str("Item ID"),expectedVersion:{type:"integer",required:true},body:{type:"string"},title:{type:"string"},category:{type:"string"},summary:{type:"string"},summaryUnchanged:{type:"boolean"},tags:{type:"array",items:{type:"string"}},aliases:{type:"array",items:{type:"string"}},needsReview:{type:"boolean"}}},
  library_search: {description:"Explicit hybrid keyword/vector search in one or more regular categories. Materials return title/summary; full body requires get.",parameters:{query:str("Query"),categories:{type:"array",required:true,items:{type:"string",enum:["认识与判断","兴趣","黑话与名词解释","资料"]}},limit:{type:"integer"},offset:{type:"integer"},tags:{type:"array",items:{type:"string"}}}},
  library_list: {description:"Browse one Library category without automatic cross-category expansion.",parameters:{category:str("Category"),limit:{type:"integer"},offset:{type:"integer"},tags:{type:"array",items:{type:"string"}}}},
  library_get: {description:"Read a Library item by stable ID, with optional bounded page.",parameters:{itemId:str("Item ID"),offset:{type:"integer"},limit:{type:"integer"},version:{type:"integer"}}},
  library_glossary: {description:"Look up jargon or aliases explicitly.",parameters:{query:{type:"string"},keywords:{type:"array",items:{type:"string"}},limit:{type:"integer"},offset:{type:"integer"}}},
  library_diary: {description:"Read independent diary records by date or ID; diaries do not participate in ordinary search.",parameters:{date:{type:"string"},id:{type:"string"},from:{type:"string"},to:{type:"string"},limit:{type:"integer"},offset:{type:"integer"}}},
  library_sources: {description:"Expand provenance into original chat or material references.",parameters:{itemId:{type:"string"},sourceRef:{type:"string"},limit:{type:"integer"},offset:{type:"integer"}}},
  library_chat_history: {description:"Read original chat in time order with references expanded and natural text assembled. No SQL required.",parameters:{date:{type:"string"},from:{type:"string"},to:{type:"string"},sessionId:{type:"string"},messageId:{type:"string"},before:{type:"integer"},after:{type:"integer"},limit:{type:"integer"},offset:{type:"integer"},cursor:{type:"string"},maxChars:{type:"integer"}}},
  library_chat_search: {description:"Search original chat text separately from ordinary memories.",parameters:{query:str("Literal search text"),from:{type:"string"},to:{type:"string"},limit:{type:"integer"},offset:{type:"integer"}}},
  library_schema: {description:"Inspect the Library SQLite schema for complex read-only queries.",parameters:{}},
  library_sql: {description:"Execute bounded read-only SQL. All writes must use unified Library tools so versions and indexes remain consistent.",parameters:{sql:str("Read-only SELECT or WITH query"),params:{type:"array",items:{type:"json"}},limit:{type:"integer"}}},
  library_mark_delete: {description:"Mark an item for deletion without permanent removal. Sources are preserved.",parameters:{itemId:str("Item ID"),expectedVersion:{type:"integer",required:true},reason:{type:"string"}}},
  library_cancel_delete: {description:"Cancel a deletion mark using the current item version.",parameters:{itemId:str("Item ID"),expectedVersion:{type:"integer",required:true}}},

  service_restart: {
    description:"Request a managed service restart. Core durably saves recovery state; accepted does not mean the restart completed.",
    parameters:{target:{type:"string",required:true,enum:["core","bridge","all"]}},
  },
  session_new: {
    description:"Request a new daily session after the current turn. Core owns the handoff and queues messages during maintenance.",
    parameters:{},
  },
  task_cancel: {
    description:"Request stopping an independent Core task. Unknown means stop is not yet proven; do not repeat its external work.",
    parameters:{taskId:str("Core task ID")},
  },
  send_message: {
    description:
      "Send a real message to the owner. Receipt is separate from acceptance.",
    parameters: { text: str("Message text") },
  },
  memory_search: {
    description:
      "Search only when needed. Supply a query and one or more explicit regular categories; never automatic recall.",
    parameters: { query: str("Keywords or natural-language query"), categories: {type:"array",required:true,items:{type:"string",enum:["认识与判断","兴趣","黑话与名词解释","资料"]}} },
  },
  library_terms: {
    description: "Explicit jargon lookup in the Library terms category, separate from ordinary memory search. Supply 1-12 non-empty keywords, at most100 characters each. No automatic lookup.",
    parameters: {
      keywords: {type:"array",required:true,items:{type:"string"},description:"1-12 keywords, each1-100 characters"},
      limit: {type:"integer",enum:Array.from({length:30},(_,index)=>index+1),default:8,description:"Result limit1-30; default8"},
      shallow: {type:"boolean",default:true,description:"Use shallow terms lookup; defaulttrue"},
    },
  },
  library_query: {
    description:
      "Read-only Library catalogue by actual category/subcategory/status and paired ISO dates, or a separate text search using query alone. Do not combine query with catalogue filters or offset. External sourceRef filtering is unsupported. No SQL.",
    parameters: {
      query: { type: "string", description: "Separate bounded text search; omit category/subcategory/status/date filters and offset" },
      offset: { type: "integer", description: "Catalogue pagination offset; not supported for text search" },
      kind: { type: "string", description: "Legacy local or memory/diary mapping; prefer actual category/subcategory" },
      sourceRef: { type: "string", description: "Development backend filter only; unsupported by the external Library SDK" },
      limit: { type: "integer" },
      category: { type: "string", enum: ["事实与经历", "认识与判断", "兴趣", "黑话与名词解释", "资料"] },
      subcategory: { type: "string", description: "Actual subcategory name returned by the Library, for example SOP" },
      status: { type: "string", enum: ["有效", "作废", "待删除"], description: "Visibility still depends on verified host identity" },
      date_start: { type: "string", description: "Full ISO calendar date YYYY-MM-DD; supply together with date_end" },
      date_end: { type: "string", description: "Full ISO calendar date YYYY-MM-DD; supply together with date_start" },
    },
  },
  library_read: {
    description:
      "Read the current full body, provenance and available version metadata by returned item ID. Version metadata does not include historical full bodies.",
    parameters: { itemId: str("Library item ID returned by search or query") },
  },
  history_read: {
    description: "Read an original raw message by the exact opaque recordId returned by the Library. Do not infer ID prefixes or substitute an item ID.",
    parameters: { recordId: str("Opaque Library original-message record ID") },
  },
  history_query: {
    description: "Read original history with sources and real timestamps.",
    parameters: {
      query: { type: "string" },
      messageId: { type: "string" },
      sessionId: { type: "string" },
      from: { type: "string" },
      to: { type: "string" },
      limit: { type: "integer" },
    },
  },
  todo_record: {
    description: "Record a todo with source message IDs.",
    parameters: {
      title: str("Todo title"),
      body: str("Todo body"),
      sourceRefs: { type: "array", items: { type: "string" } },
    },
  },
  task_create: {
    description:
      "Create a Core-owned independent long task; never spawn a daily internal subagent.",
    parameters: {
      title: str("Task title"),
      objective: str("Task objective"),
      contextRefs: { type: "array", items: { type: "string" } },
      executor: { type: "string" },
    },
  },
  task_artifact_write: {
    description: "Publish an immutable text document produced by this independent task. Returns a durable artifactRef and SHA256. Only work sessions may write.",
    parameters: {name:str("Artifact display name"),mediaType:{type:"string"},text:str("Complete artifact text")},
  },
  task_artifact_read: {
    description: "Read a bounded page of a published task artifact by its exact artifactRef. Follow nextOffset; SHA256 identifies the full UTF-8 document.",
    parameters: {artifactRef:str("Exact task-artifact reference"),offset:{type:"integer"},limit:{type:"integer"}},
  },
  task_result_read: {
    description:
      "Read a bounded page of the complete immutable task result JSON by task ID. Use resultRef and sha256 from its notification to verify provenance; follow nextOffset for continuation. Default 4096 and maximum 16000 characters are enforced by Core.",
    parameters: {
      taskId: str("Core task ID from a task-result notification"),
      offset: { type: "integer" },
      limit: { type: "integer" },
    },
  },
  task_status: {
    description: "Read a Core task status.",
    parameters: { taskId: str("Core task ID") },
  },
};
