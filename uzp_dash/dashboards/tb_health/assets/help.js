(function(){
  /* Памятка открывается при каждом запуске отчёта, пока пользователь не отметит
     «Больше не показывать». Отметка живёт в localStorage этого браузера.
     Все обращения к хранилищу — в try/catch: в приватном окне или при запрете
     данных сайта оно бросает исключение, и отчёт из-за памятки падать не должен. */
  var KEY = 'tb_health.help.hidden';
  var dlg = document.getElementById('hlp');
  var never = document.getElementById('hlp-never');
  if(!dlg) return;

  function hidden(){ try { return localStorage.getItem(KEY) === '1'; } catch(e){ return false; } }
  function remember(on){
    try { if(on){ localStorage.setItem(KEY, '1'); } else { localStorage.removeItem(KEY); } }
    catch(e){}
  }

  window.hlpOpen = function(){
    if(never) never.checked = hidden();
    if(typeof dlg.showModal === 'function'){ if(!dlg.open) dlg.showModal(); }
    else { dlg.setAttribute('open', ''); }
  };
  window.hlpClose = function(){
    if(typeof dlg.close === 'function'){ dlg.close(); } else { dlg.removeAttribute('open'); }
  };
  if(never){ never.addEventListener('change', function(){ remember(never.checked); }); }
  /* клик по подложке закрывает (по умолчанию <dialog> этого не делает) */
  dlg.addEventListener('click', function(e){ if(e.target === dlg) window.hlpClose(); });

  if(!hidden()) window.hlpOpen();
})();
