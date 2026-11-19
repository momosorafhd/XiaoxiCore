'use strict';
// Conservative DOM renderer. Raw HTML is shown as text, never interpreted.
// Covers the document structures used by the library without a CDN dependency.
function mdInline(text, parent) {
  const rx = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^\s)]+\))/g;
  let last = 0;
  for (const match of text.matchAll(rx)) {
    parent.append(document.createTextNode(text.slice(last, match.index)));
    const value = match[0];
    if (value.startsWith('`')) {const el=document.createElement('code');el.textContent=value.slice(1,-1);parent.append(el);}
    else if (value.startsWith('**')) {const el=document.createElement('strong');el.textContent=value.slice(2,-2);parent.append(el);}
    else {
      const parts=value.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const el=document.createElement('a');el.textContent=parts[1];
      try {const url=new URL(parts[2],location.href);if(['http:','https:','mailto:'].includes(url.protocol)){el.href=url.href;el.target='_blank';el.rel='noopener noreferrer';}else el.textContent=value;}
      catch {el.textContent=value;}
      parent.append(el);
    }
    last=match.index+value.length;
  }
  parent.append(document.createTextNode(text.slice(last)));
}
function renderMarkdown(source, container, toc) {
  container.replaceChildren();if(toc)toc.replaceChildren();
  const lines=String(source||'').replace(/\r/g,'').split('\n'),headings=[];
  let i=0;
  const inline=(tag,text)=>{const e=document.createElement(tag);mdInline(text,e);return e;};
  while(i<lines.length){
    const s=lines[i];
    if(!s.trim()){i++;continue;}
    if(/^```/.test(s)){const pre=document.createElement('pre'),code=document.createElement('code');const lang=s.slice(3).trim();i++;const buf=[];while(i<lines.length&&!/^```/.test(lines[i]))buf.push(lines[i++]);if(i<lines.length)i++;code.textContent=buf.join('\n');pre.append(code);if(lang)pre.setAttribute('aria-label',lang+' 代码');container.append(pre);continue;}
    const h=s.match(/^(#{1,4})\s+(.+)$/);
    if(h){const el=inline('h'+h[1].length,h[2]);el.id='heading-'+headings.length;container.append(el);if(h[1].length<=3)headings.push({id:el.id,text:h[2],level:h[1].length});i++;continue;}
    if(/^\s*([-*_])\1\1+\s*$/.test(s)){container.append(document.createElement('hr'));i++;continue;}
    if(/^>\s?/.test(s)){const b=document.createElement('blockquote'),buf=[];while(i<lines.length&&/^>/.test(lines[i]))buf.push(lines[i++].replace(/^>\s?/,''));b.append(inline('p',buf.join(' ')));container.append(b);continue;}
    if(i+1<lines.length&&s.includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])){
      const wrapper=document.createElement('div');wrapper.className='table-scroll';const table=document.createElement('table'),thead=document.createElement('thead'),tr=document.createElement('tr');
      const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(v=>v.trim());
      cells(s).forEach(t=>tr.append(inline('th',t)));thead.append(tr);table.append(thead);i+=2;const body=document.createElement('tbody');while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){const row=document.createElement('tr');cells(lines[i++]).forEach(t=>row.append(inline('td',t)));body.append(row);}table.append(body);wrapper.append(table);container.append(wrapper);continue;
    }
    const list=s.match(/^\s*([-*]|\d+\.)\s+(.+)$/);
    if(list){const ordered=/\d/.test(list[1]),el=document.createElement(ordered?'ol':'ul');while(i<lines.length){const l=lines[i].match(/^\s*([-*]|\d+\.)\s+(.+)$/);if(!l||/\d/.test(l[1])!==ordered)break;el.append(inline('li',l[2]));i++;}container.append(el);continue;}
    const buf=[s];i++;while(i<lines.length&&lines[i].trim()&&!/^(#{1,4}\s|```|>|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i]))buf.push(lines[i++]);container.append(inline('p',buf.join('\n')));
  }
  if(toc){toc.hidden=headings.length<3;if(!toc.hidden){const title=document.createElement('div');title.className='toc-heading';title.textContent='本篇目录';toc.append(title);for(const h of headings){const a=document.createElement('a');a.href='#'+h.id;a.textContent=h.text;a.style.paddingLeft=(h.level-1)*7+'px';a.onclick=e=>{e.preventDefault();document.getElementById(h.id)?.scrollIntoView({behavior:'smooth',block:'start'});};toc.append(a);}}}
}
