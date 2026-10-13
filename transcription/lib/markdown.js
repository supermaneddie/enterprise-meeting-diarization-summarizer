import { marked } from 'marked';
import DOMPurify from 'dompurify';

export function render(element, source) {
  element.innerHTML = DOMPurify.sanitize(marked.parse(source, { gfm: true, async: false }), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form'],
    FORBID_ATTR: ['style'],
  });
}
