import { snapshot } from './coordination.mjs';

// Text is rendered and frozen by Core. Bridge owns only placement and wire validation.
export function normalizePromptSections(value = []) {
  if (!Array.isArray(value) || value.length > 100) throw new Error('Invalid promptSections');
  const ids = new Set(); let chars = 0;
  return value.map(section => {
    if (!section || typeof section.id !== 'string' || !section.id || ids.has(section.id) ||
        typeof section.text !== 'string' || !['system','start','run'].includes(section.placement) ||
        !['string','number'].includes(typeof section.version)) throw new Error('Invalid prompt section');
    ids.add(section.id); chars += section.text.length;
    if (chars > 1000000) throw new Error('Prompt sections exceed1000000 characters');
    return snapshot(section);
  });
}
export function sectionText(sections, placement) {
  return (sections ?? []).filter(s => s.placement === placement).map(s => s.text).filter(Boolean).join('\n\n');
}
export function presentation(input) {
  if (input.presentationText !== undefined) {
    if (typeof input.presentationText !== 'string') throw new Error('presentationText must be string');
    return input.presentationText;
  }
  // Legacy direct clients retain their exact raw text; Core supplies natural metadata rendering.
  return input.text;
}
