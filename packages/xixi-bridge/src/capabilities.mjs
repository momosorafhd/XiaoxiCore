// Library availability is frozen by the host, never selected in model arguments.
export const LIBRARY_CORE_TOOLS = [
  'memory_search', 'library_terms', 'library_query', 'library_read',
  'todo_record', 'history_query', 'history_read',
  'library_record','library_revise','library_search','library_list','library_get','library_glossary','library_diary','library_sources','library_chat_history','library_chat_search','library_schema','library_sql','library_mark_delete','library_cancel_delete',
];
export const DEFERRED_CORE_TOOLS = [...LIBRARY_CORE_TOOLS, 'session_new'];
export function validateLibraryMode(mode) {
  if (mode !== undefined && !['external', 'development', 'deferred'].includes(mode))
    throw new Error('Invalid libraryMode');
  return mode;
}
export function isLibraryToolName(name) {
  return LIBRARY_CORE_TOOLS.includes(name) || /^(?:library|memory|history|todo)(?:[_.-]|$)/i.test(name);
}
export function libraryDeferred(config) {
  return config?.libraryMode === 'deferred';
}
export function requireAvailableMaintenance(config) {
  if (libraryDeferred(config) && ['idle', 'dream', 'maintenance'].includes(config.kind))
    throw new Error('LIBRARY_DEFERRED_MAINTENANCE_UNAVAILABLE');
}
