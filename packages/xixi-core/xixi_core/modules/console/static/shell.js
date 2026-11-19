'use strict';
(()=>{
 const by=id=>document.getElementById(id),page=document.body.dataset.page||location.pathname;
 let compact=false;try{compact=page==='library'&&localStorage.getItem('xixi-layout:'+page)==='compact';}catch{}
 function layout(){document.body.dataset.density=compact?'compact':'cards';for(const b of document.querySelectorAll('.layout-toggle')){b.textContent=compact?'▦ 方块卡片':'▤ 紧凑长条';b.setAttribute('aria-label',compact?'切换为方块卡片':'切换为紧凑长条');b.setAttribute('aria-pressed',String(compact));}}
 for(const b of document.querySelectorAll('.layout-toggle'))b.addEventListener('click',()=>{compact=!compact;try{localStorage.setItem('xixi-layout:'+page,compact?'compact':'cards');}catch{}layout();});layout();
 const mobile=matchMedia('(max-width:800px)'),scope=document.querySelector('.scope-control'),scopeMarker=document.createComment('search-scope');if(scope)scope.before(scopeMarker);
 function syncNav(){const hidden=mobile.matches&&!document.body.classList.contains('nav-open');if(by('global-nav')){by('global-nav').inert=hidden;by('global-nav').setAttribute('aria-hidden',String(hidden));}by('open-nav')?.setAttribute('aria-expanded',String(!hidden));if(scope&&by('filters')){if(mobile.matches)by('filters').prepend(scope);else scopeMarker.after(scope);}}
 function closeNav(){document.body.classList.remove('nav-open');if(by('nav-shade'))by('nav-shade').hidden=true;syncNav();}
 by('open-nav')?.addEventListener('click',()=>{document.body.classList.add('nav-open');by('nav-shade').hidden=false;syncNav();});by('close-nav')?.addEventListener('click',closeNav);by('nav-shade')?.addEventListener('click',closeNav);mobile.addEventListener('change',closeNav);syncNav();
 document.addEventListener('keydown',e=>{if(e.key==='Escape')closeNav();});
 let timer; if(typeof notify==='function'&&page!=='library')notify=function(message,bad=false){const el=by('message');if(!el)return;clearTimeout(timer);el.textContent=message;el.className='toast'+(bad?' bad':'');el.hidden=!message;if(message)timer=setTimeout(()=>el.hidden=true,bad?10000:4500);};
 // The original file browser links to the former single-page credentials section.
 const secretLink=document.querySelector('.secret-link');if(secretLink)secretLink.href=(globalThis.XIXI_ADMIN_BASE||'')+'/credentials';
 const message=by('message');if(message)new MutationObserver(()=>{message.hidden=!message.textContent.trim();}).observe(message,{childList:true,subtree:true,characterData:true});
})();
