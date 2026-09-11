
(function(){
  var SECTION_DESCRIPTIONS = {
    'Матрица выполнения ГОСБ/сегмент': 'Показывает, какие ГОСБ и сегменты не дотягивают до плана — чтобы сразу увидеть, где проблема, прежде чем переходить в детали.',
    'Матрица выполнения ТБ/сегмент': 'Показывает, какие территориальные банки и сегменты не дотягивают до плана — чтобы сразу увидеть, где проблема, прежде чем переходить в детали.',
    'Детализация по ГОСБ': 'Карточка на каждый ГОСБ: план, факт и что делать. Клик по карточке открывает разбор вплоть до конкретных организаций-получателей.',
    'Детализация по ТБ': 'Карточка на каждый территориальный банк: план, факт и что делать. Клик по карточке открывает разбор вплоть до конкретных организаций-получателей.',
    'Потенциал организаций': 'Список конкретных организаций, по которым нужно отработать отток или привлечение, чтобы закрыть разрыв до плана.'
  };

  function currentLevel(){
    return document.querySelector('.lvl:not([hidden])');
  }

  /* открывающиеся по клику диалоги (карточки ГОСБ/ТБ) могут лежать внутри
     свёрнутого нативного <details> раздела — сама по себе showModal() тогда
     ничего не покажет, поэтому перед открытием разворачиваем предков */
  var _origGdOpen = window.gdOpen;
  window.gdOpen = function(id){
    var dlg = document.getElementById('gd-' + id);
    if(dlg){
      var p = dlg.parentElement;
      while(p){
        if(p.tagName === 'DETAILS' && !p.open){ p.open = true; }
        p = p.parentElement;
      }
    }
    if(typeof _origGdOpen === 'function'){ _origGdOpen(id); }
  };

  function getTabsMap(){
    var map = {};
    Array.prototype.forEach.call(document.querySelectorAll('.lvl-tab'), function(btn){
      var idx = parseInt(btn.getAttribute('data-lvl'), 10);
      map[idx] = btn.textContent.trim();
    });
    return map;
  }

  /* 0. Колонка фильтров: якоря разделов текущего уровня (переключение банков — см. buildTopSwitchers) */
  function buildSidebarLayout(){
    if(document.body.dataset.sidebarBuilt === '1') return;
    var wrap = document.querySelector('.wrap');
    var nav = wrap ? wrap.querySelector(':scope > nav.lvls') : null;
    if(!wrap || !nav){ document.body.dataset.sidebarBuilt = '1'; return; }

    var layout = document.createElement('div');
    layout.className = 'app-layout';
    var sidebar = document.createElement('div');
    sidebar.className = 'app-sidebar';
    var main = document.createElement('div');
    main.className = 'app-main';

    sidebar.appendChild(nav); /* остаётся в DOM для чтения названий банков, визуально скрыт */

    var firstScript = wrap.querySelector(':scope > script');
    var lvlEls = Array.prototype.slice.call(wrap.querySelectorAll(':scope > .lvl'));
    lvlEls.forEach(function(el){ main.appendChild(el); });

    layout.appendChild(sidebar);
    layout.appendChild(main);
    if(firstScript){ wrap.insertBefore(layout, firstScript); }
    else { wrap.appendChild(layout); }

    document.body.dataset.sidebarBuilt = '1';
  }

  /* 0b. Компактные выпадающие списки: территориальный банк и отдельно — ГОСБ по всем банкам */
  /* Универсальное поле «поиск + список» с фильтрацией по подстроке —
     собственная реализация вместо нативного <datalist>, у которого поиск
     по вхождению работает не во всех браузерах предсказуемо. */
  function buildSearchField(labelText, placeholderText, items){
    var wrap = document.createElement('label');
    wrap.className = 'ts-item';
    var span = document.createElement('span');
    span.className = 'ts-label';
    span.textContent = labelText;
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'ts-select';
    input.placeholder = placeholderText;
    input.autocomplete = 'off';
    var dropdown = document.createElement('div');
    dropdown.className = 'ts-dropdown';

    var currentMatches = [];

    function renderList(query){
      dropdown.innerHTML = '';
      var q = query.trim().toLowerCase();
      currentMatches = items.filter(function(it){
        return !q || it.label.toLowerCase().indexOf(q) !== -1;
      }).slice(0, 40);

      if(!currentMatches.length){
        var empty = document.createElement('div');
        empty.className = 'ts-empty';
        empty.textContent = 'Совпадений не найдено';
        dropdown.appendChild(empty);
        return;
      }
      currentMatches.forEach(function(it){
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ts-opt';
        btn.textContent = it.label;
        if(it.sublabel){
          var sub = document.createElement('span');
          sub.className = 'ts-opt-group';
          sub.textContent = it.sublabel;
          btn.appendChild(sub);
        }
        btn.addEventListener('mousedown', function(e){
          e.preventDefault();
          it.onSelect();
          input.value = '';
          closeDropdown();
        });
        dropdown.appendChild(btn);
      });
    }

    function openDropdown(){ renderList(input.value); dropdown.classList.add('open'); }
    function closeDropdown(){ dropdown.classList.remove('open'); }

    input.addEventListener('focus', openDropdown);
    input.addEventListener('input', openDropdown);
    input.addEventListener('blur', function(){ setTimeout(closeDropdown, 150); });
    input.addEventListener('keydown', function(e){
      if(e.key === 'Escape'){ closeDropdown(); input.blur(); }
      else if(e.key === 'Enter'){
        e.preventDefault();
        if(currentMatches[0]){
          currentMatches[0].onSelect();
          input.value = '';
          closeDropdown();
        }
      }
    });

    wrap.appendChild(span);
    wrap.appendChild(input);
    wrap.appendChild(dropdown);
    return { root: wrap, input: input };
  }

  function buildTopSwitchers(){
    if(document.body.dataset.switchersBuilt === '1') return;
    var header = document.querySelector('.wrap > header');
    if(!header){ document.body.dataset.switchersBuilt = '1'; return; }
    var tabsMap = getTabsMap();
    if(!Object.keys(tabsMap).length){ document.body.dataset.switchersBuilt = '1'; return; }

    var wrap = document.createElement('div');
    wrap.className = 'top-switchers';

    /* --- ТБ: поле всегда пустое, ничего стирать не нужно --- */
    var tbItems = Object.keys(tabsMap).map(Number).sort(function(a, b){ return a - b; }).map(function(idx){
      return {
        label: idx === 0 ? 'СБ (главный экран)' : tabsMap[idx],
        onSelect: function(){ window.lvlGo(idx); }
      };
    });
    var tbField = buildSearchField('Территориальный банк', 'Начните вводить название банка…', tbItems);
    tbField.input.id = 'tbInput';

    /* --- ГОСБ: поиск по всем банкам сразу, с подсказкой банка-владельца --- */
    var gItems = [];
    for(var i = 1; i <= 11; i++){
      var lvlEl = document.getElementById('lvl-' + i);
      if(!lvlEl) continue;
      var cards = lvlEl.querySelectorAll('section .gcards > .gcard');
      Array.prototype.forEach.call(cards, function(card){
        var raw = card.getAttribute('onclick') || '';
        var m = raw.match(/gdOpen\('([^']+)'\)/);
        var h3 = card.querySelector('h3');
        if(!m || !h3) return;
        var name = h3.textContent.trim();
        var level = i;
        var id = m[1];
        gItems.push({
          label: name,
          sublabel: tabsMap[level] || ('Банк ' + level),
          onSelect: function(){
            window.lvlGo(level);
            setTimeout(function(){
              if(typeof window.gdOpen === 'function'){ window.gdOpen(id); }
            }, 60);
          }
        });
      });
    }
    var gField = buildSearchField('ГОСБ', 'Начните вводить название ГОСБ…', gItems);

    wrap.appendChild(tbField.root);
    wrap.appendChild(gField.root);
    header.insertAdjacentElement('afterend', wrap);

    document.body.dataset.switchersBuilt = '1';
  }

  /* 0c. Кнопки возврата внутри разбора ГОСБ — на главный экран и к своему ТБ */
  function addDialogBackNav(){
    if(document.body.dataset.backnavAdded === '1') return;
    var tabsMap = getTabsMap();
    var dialogs = document.querySelectorAll('dialog.gd[id^="gd-"]');
    dialogs.forEach(function(dialog){
      var m = dialog.id.match(/^gd-(\d+)-/);
      if(!m) return;
      var level = parseInt(m[1], 10);
      if(level === 0) return; /* это разбор ТБ на главном экране — возврат не нужен */
      var head = dialog.querySelector('.gd-head');
      if(!head) return;
      var bankName = tabsMap[level] || ('банк ' + level);

      var nav = document.createElement('div');
      nav.className = 'gd-backnav';

      var homeBtn = document.createElement('button');
      homeBtn.type = 'button';
      homeBtn.className = 'gd-back-btn';
      homeBtn.textContent = '\u2190 Главный экран';
      homeBtn.addEventListener('click', function(){ dialog.close(); window.lvlGo(0); });

      var bankBtn = document.createElement('button');
      bankBtn.type = 'button';
      bankBtn.className = 'gd-back-btn';
      bankBtn.textContent = '\u2190 ' + bankName;
      bankBtn.addEventListener('click', function(){ dialog.close(); window.lvlGo(level); });

      nav.appendChild(homeBtn);
      nav.appendChild(bankBtn);
      head.insertAdjacentElement('afterend', nav);
    });
    document.body.dataset.backnavAdded = '1';
  }

  /* 1. Единая плашка: hero + КПЭ-грид оборачиваются в общий контейнер */
  function wrapForecastPanel(lvlEl){
    if(lvlEl.dataset.panelWrapped === '1') return;
    var hero = lvlEl.querySelector(':scope > .card.hero');
    if(!hero){ lvlEl.dataset.panelWrapped = '1'; return; }
    var grid = hero.nextElementSibling;
    if(!grid || !grid.classList || !grid.classList.contains('grid') || !grid.classList.contains('cols-2')){
      lvlEl.dataset.panelWrapped = '1'; return;
    }
    var panel = document.createElement('div');
    panel.className = 'forecast-panel';
    hero.parentNode.insertBefore(panel, hero);
    panel.appendChild(hero);
    panel.appendChild(grid);
    lvlEl.dataset.panelWrapped = '1';
  }

  /* 2. «Управление портфелем» — всплывающее окно с кнопкой-триггером на плашке прогноза */
  function convertMgmtToModal(lvlEl){
    if(lvlEl.dataset.mgmtModal === '1') return;
    var sections = Array.prototype.slice.call(lvlEl.querySelectorAll(':scope > section'));
    var mgmtSection = sections.filter(function(sec){
      var h2 = sec.querySelector('h2');
      return h2 && h2.textContent.trim() === 'Управление портфелем';
    })[0];
    if(!mgmtSection){ lvlEl.dataset.mgmtModal = '1'; return; }

    var dialog = document.createElement('dialog');
    dialog.className = 'gd';
    dialog.id = 'mgmt-' + lvlEl.id;

    var sheet = document.createElement('div');
    sheet.className = 'gd-sheet';
    var head = document.createElement('div');
    head.className = 'gd-head';
    var titleWrap = document.createElement('div');
    var h3 = document.createElement('h3');
    h3.style.margin = '0';
    h3.textContent = 'Управление портфелем';
    var note = document.createElement('div');
    note.className = 'gd-note';
    note.textContent = 'справочно · факт по портфелю, оттоку и пайплайну';
    titleWrap.appendChild(h3);
    titleWrap.appendChild(note);
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'gd-close';
    closeBtn.setAttribute('aria-label', 'Закрыть');
    closeBtn.textContent = '\u00d7';
    closeBtn.addEventListener('click', function(){ dialog.close(); });
    head.appendChild(titleWrap);
    head.appendChild(closeBtn);
    sheet.appendChild(head);

    Array.prototype.slice.call(mgmtSection.children).forEach(function(el){
      if(el.tagName === 'H2') return;
      if(el.classList && el.classList.contains('eyebrow')) return;
      sheet.appendChild(el);
    });

    dialog.appendChild(sheet);
    document.body.appendChild(dialog);
    dialog.addEventListener('click', function(e){ if(e.target === dialog) dialog.close(); });

    mgmtSection.remove();

    var panel = lvlEl.querySelector('.forecast-panel');
    if(panel){
      var trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'mgmt-trigger';
      trigger.innerHTML = '<span>Управление портфелем: потери и прирост</span><span class="mti">\u2192</span>';
      trigger.addEventListener('click', function(){ dialog.showModal(); });
      panel.appendChild(trigger);
    }

    lvlEl.dataset.mgmtModal = '1';
  }

  /* 3. Плашка прогноза сворачивается отдельно, открыта по умолчанию */
  function makeForecastCollapsible(lvlEl){
    if(lvlEl.dataset.forecastCollapsible === '1') return;
    var panel = lvlEl.querySelector('.forecast-panel');
    if(!panel){ lvlEl.dataset.forecastCollapsible = '1'; return; }
    var hero = panel.querySelector(':scope > .card.hero');
    var grid = panel.querySelector(':scope > .grid.cols-2');
    if(!hero || !grid){ lvlEl.dataset.forecastCollapsible = '1'; return; }

    var toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'forecast-toggle open';
    toggle.title = 'Нажмите, чтобы свернуть показатели получателей и ФОТ';
    toggle.innerHTML = '<span class="st-ico">\u25b8</span><span class="st-label">Свернуть показатели</span>';
    hero.appendChild(toggle);

    toggle.addEventListener('click', function(){
      var isOpen = grid.style.display !== 'none';
      grid.style.display = isOpen ? 'none' : '';
      toggle.classList.toggle('open', !isOpen);
      toggle.querySelector('.st-label').textContent = isOpen ? 'Показать показатели' : 'Свернуть показатели';
      toggle.title = isOpen ? 'Нажмите, чтобы показать показатели получателей и ФОТ' : 'Нажмите, чтобы свернуть показатели получателей и ФОТ';
    });

    lvlEl.dataset.forecastCollapsible = '1';
  }

  /* 4. Подстрочные описания разделов */
  function addSectionDescriptions(lvlEl){
    if(lvlEl.dataset.descAdded === '1') return;
    var heads = lvlEl.querySelectorAll(':scope > section > h2');
    heads.forEach(function(h){
      var text = h.textContent.trim();
      var desc = SECTION_DESCRIPTIONS[text];
      if(!desc) return;
      var p = document.createElement('p');
      p.className = 'section-desc';
      p.textContent = desc;
      h.insertAdjacentElement('afterend', p);
    });
    var panel = lvlEl.querySelector('.forecast-panel');
    if(panel){
      var hero = panel.querySelector(':scope > .card.hero');
      if(hero){
        var p2 = document.createElement('p');
        p2.className = 'section-desc section-desc-hero';
        p2.textContent = 'Итоговый прогноз выполнения плана по получателям и ФОТ на ближайший месяц — ключевая цифра для оценки текущего состояния портфеля.';
        hero.insertBefore(p2, hero.querySelector('.forecast-toggle') || null);
      }
    }
    lvlEl.dataset.descAdded = '1';
  }

  /* 5. Разделы становятся нативными <details>/<summary> — свёрнуты по умолчанию,
        служебные эйбров-подписи («Диагностика по прогнозу» и т.п.) убираются как избыточные */
  function sectionsToDetails(lvlEl){
    if(lvlEl.dataset.detailsBuilt === '1') return;
    var sections = lvlEl.querySelectorAll(':scope > section');
    sections.forEach(function(sec){
      var h2 = sec.querySelector(':scope > h2');
      if(!h2) return;
      var eyebrow = sec.querySelector(':scope > .eyebrow');
      if(eyebrow) eyebrow.remove();
      var desc = h2.nextElementSibling && h2.nextElementSibling.classList.contains('section-desc')
        ? h2.nextElementSibling : null;

      var details = document.createElement('details');
      details.className = 'section-details';

      var summary = document.createElement('summary');
      summary.title = 'Нажмите, чтобы развернуть или свернуть раздел';

      var row = document.createElement('div');
      row.className = 'h2-row';
      var chevron = document.createElement('span');
      chevron.className = 'st-chevron';
      chevron.textContent = '\u203a';
      var textCol = document.createElement('div');
      textCol.appendChild(h2);
      row.appendChild(chevron);
      row.appendChild(textCol);
      summary.appendChild(row);
      if(desc) summary.appendChild(desc);

      var hint = document.createElement('span');
      hint.className = 'section-hint';
      hint.textContent = 'Нажмите, чтобы развернуть';
      summary.appendChild(hint);

      details.appendChild(summary);

      var body = document.createElement('div');
      body.className = 'section-body';
      Array.prototype.slice.call(sec.children).forEach(function(el){ body.appendChild(el); });
      details.appendChild(body);

      sec.appendChild(details);

      details.addEventListener('toggle', function(){
        hint.textContent = details.open ? 'Нажмите, чтобы свернуть' : 'Нажмите, чтобы развернуть';
        updateProgress(lvlEl);
      });
    });
    lvlEl.dataset.detailsBuilt = '1';
  }

  /* 6. Поиск/переход к разбору конкретного ГОСБ внутри карточек текущего уровня */
  function addGosbFinder(lvlEl){
    if(lvlEl.dataset.finderAdded === '1') return;
    var gcardsDiv = lvlEl.querySelector(':scope section .gcards');
    if(!gcardsDiv){ lvlEl.dataset.finderAdded = '1'; return; }
    var cards = Array.prototype.slice.call(gcardsDiv.querySelectorAll(':scope > .gcard'));
    if(!cards.length){ lvlEl.dataset.finderAdded = '1'; return; }

    var items = cards.map(function(card){
      var raw = card.getAttribute('onclick') || '';
      var m = raw.match(/gdOpen\('([^']+)'\)/);
      var h3 = card.querySelector('h3');
      return { id: m ? m[1] : null, name: h3 ? h3.textContent.trim() : '', el: card };
    }).filter(function(it){ return it.id; });

    var finder = document.createElement('div');
    finder.className = 'gosb-finder';
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'gf-input';
    input.placeholder = 'Найти по названию…';
    var select = document.createElement('select');
    select.className = 'gf-select';
    var opt0 = document.createElement('option');
    opt0.value = '';
    opt0.textContent = 'Перейти к разбору \u2192';
    select.appendChild(opt0);

    items.slice().sort(function(a, b){ return a.name.localeCompare(b.name, 'ru'); }).forEach(function(it){
      var o = document.createElement('option');
      o.value = it.id;
      o.textContent = it.name;
      select.appendChild(o);
    });

    finder.appendChild(input);
    finder.appendChild(select);
    gcardsDiv.parentNode.insertBefore(finder, gcardsDiv);

    input.addEventListener('input', function(){
      var q = input.value.trim().toLowerCase();
      items.forEach(function(it){
        it.el.style.display = (!q || it.name.toLowerCase().indexOf(q) !== -1) ? '' : 'none';
      });
    });

    select.addEventListener('change', function(){
      var id = select.value;
      if(!id) return;
      var it = items.filter(function(x){ return x.id === id; })[0];
      if(it){
        it.el.style.display = '';
        it.el.scrollIntoView({behavior:'smooth', block:'center'});
        if(typeof window.gdOpen === 'function'){ window.gdOpen(id); }
      }
      select.value = '';
    });

    lvlEl.dataset.finderAdded = '1';
  }

  /* 7. Прогресс просмотра разделов текущего уровня */
  function ensureProgressEl(){
    var el = document.getElementById('read-progress');
    if(el) return el;
    var sidebar = document.querySelector('.app-sidebar');
    if(!sidebar) return null;
    el = document.createElement('div');
    el.id = 'read-progress';
    el.className = 'read-progress';
    el.hidden = true;
    el.innerHTML =
      '<div class="rp-text"></div>' +
      '<div class="rp-bar"><span></span></div>' +
      '<div class="rp-hint">Разверните все разделы, чтобы увидеть полную картину</div>';
    sidebar.appendChild(el);
    return el;
  }

  function updateProgress(lvlEl){
    var detailsEls = lvlEl.querySelectorAll(':scope > section > .section-details');
    var total = detailsEls.length;
    var opened = 0;
    detailsEls.forEach(function(d){ if(d.open) opened++; });

    var prog = ensureProgressEl();
    if(prog){
      if(total){
        prog.hidden = false;
        prog.querySelector('.rp-text').textContent = 'Просмотрено разделов: ' + opened + ' из ' + total;
        prog.querySelector('.rp-bar > span').style.width = (opened / total * 100) + '%';
      } else {
        prog.hidden = true;
      }
    }

    var bar = document.getElementById('quicknav');
    if(bar){
      var pills = bar.querySelectorAll('.qn-pill[data-sec-idx]');
      pills.forEach(function(p){
        var idx = parseInt(p.getAttribute('data-sec-idx'), 10);
        var det = detailsEls[idx];
        if(det){ p.classList.toggle('qn-done', det.open); }
      });
      var forecastPill = bar.querySelector('.qn-pill[data-forecast]');
      if(forecastPill){
        var panel = lvlEl.querySelector('.forecast-panel');
        var grid = panel ? panel.querySelector(':scope > .grid.cols-2') : null;
        var isOpen = !grid || grid.style.display !== 'none';
        forecastPill.classList.toggle('qn-done', isOpen);
      }
    }
  }

  /* 8. Быстрая навигация по разделам текущего уровня (в сайдбаре) */
  function buildQuickNav(lvlEl){
    var bar = document.getElementById('quicknav');
    if(!bar){
      bar = document.createElement('div');
      bar.id = 'quicknav';
      bar.className = 'quicknav';
      var sidebar = document.querySelector('.app-sidebar');
      if(sidebar){
        var t2 = document.createElement('div');
        t2.className = 'app-sidebar-title';
        t2.textContent = 'Разделы страницы';
        sidebar.appendChild(t2);
        sidebar.appendChild(bar);
      } else {
        var lvls = document.querySelector('.lvls');
        lvls.insertAdjacentElement('afterend', bar);
      }
    }
    bar.innerHTML = '';

    var heroHost = lvlEl.querySelector('.forecast-panel') || lvlEl.querySelector(':scope > .card.hero');
    if(heroHost){
      var b0 = document.createElement('button');
      b0.type = 'button';
      b0.className = 'qn-pill';
      b0.setAttribute('data-forecast', '1');
      var b0dot = document.createElement('span');
      b0dot.className = 'qn-dot';
      var b0label = document.createElement('span');
      b0label.textContent = 'Прогноз выполнения';
      b0.appendChild(b0label);
      b0.appendChild(b0dot);
      b0.title = 'Перейти к прогнозу и развернуть показатели';
      b0.addEventListener('click', function(){
        heroHost.scrollIntoView({behavior:'smooth', block:'start'});
        var toggle = heroHost.querySelector('.forecast-toggle');
        var grid = heroHost.querySelector(':scope > .grid.cols-2');
        if(toggle && grid && grid.style.display === 'none'){
          toggle.click();
        }
      });
      bar.appendChild(b0);
    }

    var detailsEls = lvlEl.querySelectorAll(':scope > section > .section-details');
    detailsEls.forEach(function(det, idx){
      var h2 = det.querySelector('h2');
      if(!h2) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'qn-pill';
      btn.setAttribute('data-sec-idx', idx);
      var dot = document.createElement('span');
      dot.className = 'qn-dot';
      var label = document.createElement('span');
      label.textContent = h2.textContent;
      btn.appendChild(label);
      btn.appendChild(dot);
      btn.title = 'Перейти к разделу и развернуть его';
      btn.addEventListener('click', function(){
        det.scrollIntoView({behavior:'smooth', block:'start'});
        if(!det.open) det.open = true;
      });
      bar.appendChild(btn);
    });

    updateProgress(lvlEl);
  }

  /* 0d. Реструктуризация карточки ГОСБ: «Портфель, потери и приход» уходит
        во всплывающее окно-кнопку (как на главной странице), «Разбор по
        сегментам» и «Отток по причинам» поднимаются в начало карточки */
  function restructureGosbDialogs(){
    if(document.body.dataset.gosbRestructured === '1') return;
    var dialogs = document.querySelectorAll('dialog.gd[id^="gd-"]');
    dialogs.forEach(function(dialog){
      var sheet = dialog.querySelector('.gd-sheet');
      if(!sheet) return;
      var blocks = Array.prototype.slice.call(sheet.querySelectorAll(':scope > .gd-block'));
      if(!blocks.length) return;

      var mgmtBlock = null, segBlock = null, outflowBlock = null;
      var others = [];
      blocks.forEach(function(b){
        var h4 = b.querySelector(':scope > h4');
        var txt = h4 ? h4.textContent.trim() : '';
        if(txt.indexOf('Портфель, потери и приход') === 0){ mgmtBlock = b; }
        else if(txt.indexOf('Разбор по сегментам') === 0){ segBlock = b; }
        else if(txt.indexOf('Отток по причинам') === 0){ outflowBlock = b; }
        else { others.push(b); }
      });

      var head = sheet.querySelector(':scope > .gd-head');

      if(mgmtBlock){
        var subDialog = document.createElement('dialog');
        subDialog.className = 'gd';
        subDialog.id = dialog.id + '-mgmt';
        var subSheet = document.createElement('div');
        subSheet.className = 'gd-sheet';
        var subHead = document.createElement('div');
        subHead.className = 'gd-head';
        var subTitleWrap = document.createElement('div');
        var subH3 = document.createElement('h3');
        subH3.style.margin = '0';
        subH3.textContent = 'Управление портфелем';
        var subNote = document.createElement('div');
        subNote.className = 'gd-note';
        subNote.textContent = 'портфель, потери и приход';
        subTitleWrap.appendChild(subH3);
        subTitleWrap.appendChild(subNote);
        var subClose = document.createElement('button');
        subClose.type = 'button';
        subClose.className = 'gd-close';
        subClose.setAttribute('aria-label', 'Закрыть');
        subClose.textContent = '\u00d7';
        subClose.addEventListener('click', function(){ subDialog.close(); });
        subHead.appendChild(subTitleWrap);
        subHead.appendChild(subClose);
        subSheet.appendChild(subHead);
        mgmtBlock.style.marginTop = '0';
        mgmtBlock.style.paddingTop = '0';
        mgmtBlock.style.borderTop = '0';
        subSheet.appendChild(mgmtBlock);
        subDialog.appendChild(subSheet);
        document.body.appendChild(subDialog);
        subDialog.addEventListener('click', function(e){ if(e.target === subDialog) subDialog.close(); });

        var trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'mgmt-trigger';
        trigger.innerHTML = '<span>Управление портфелем: потери и прирост</span><span class="mti">\u2192</span>';
        trigger.addEventListener('click', function(){ subDialog.showModal(); });

        if(head){ head.insertAdjacentElement('afterend', trigger); }
        else { sheet.insertBefore(trigger, sheet.firstChild); }
      }

      var orderedRest = [];
      if(segBlock) orderedRest.push(segBlock);
      if(outflowBlock) orderedRest.push(outflowBlock);
      others.forEach(function(b){ orderedRest.push(b); });
      orderedRest.forEach(function(b){ sheet.appendChild(b); });
    });
    document.body.dataset.gosbRestructured = '1';
  }

  /* 0e. Крупнее % выполнения на плашке прогноза: строка ФОТ и «(N% плана)»
        рядом с «год к году» — оформляются как значимые цифры */
  function emphasizeForecastNumbers(lvlEl){
    if(lvlEl.dataset.numEmph === '1') return;
    var panel = lvlEl.querySelector('.forecast-panel');
    if(!panel){ lvlEl.dataset.numEmph = '1'; return; }
    var hero = panel.querySelector(':scope > .card.hero');
    if(hero){
      var row2s = hero.querySelectorAll(':scope > .row2');
      if(row2s.length){
        var fot = row2s[0];
        fot.classList.add('row2-fot');
        fot.innerHTML = fot.innerHTML.replace(/(\d+(?:[.,]\d+)?%)/, '<b class="row2-num">$1</b>');
      }
    }
    panel.querySelectorAll(':scope > .grid.cols-2 .kpi .foot').forEach(function(foot){
      var deltaB = foot.parentElement ? foot.parentElement.querySelector('.delta b') : null;
      var colorStyle = deltaB ? deltaB.getAttribute('style') : null;
      var attr = colorStyle ? ' style="' + colorStyle + '"' : '';
      foot.innerHTML = foot.innerHTML.replace(
        /\((\d+(?:[.,]\d+)?%\s*плана)\)/,
        '(<b' + attr + '>$1</b>)'
      );
    });
    lvlEl.dataset.numEmph = '1';
  }

  function enhanceLevel(lvlEl){
    wrapForecastPanel(lvlEl);
    convertMgmtToModal(lvlEl);
    makeForecastCollapsible(lvlEl);
    addSectionDescriptions(lvlEl);
    emphasizeForecastNumbers(lvlEl);
    sectionsToDetails(lvlEl);
    addGosbFinder(lvlEl);
    buildQuickNav(lvlEl);
  }

  function refresh(){
    buildSidebarLayout();
    buildTopSwitchers();
    restructureGosbDialogs();
    addDialogBackNav();
    var lvl = currentLevel();
    if(lvl) enhanceLevel(lvl);
  }

  document.addEventListener('click', function(e){
    if(e.target.closest('.lvl-tab') || e.target.closest('.lvl-back button') || e.target.closest('.g-drill')){
      setTimeout(refresh, 0);
    }
  });
  window.addEventListener('hashchange', function(){ setTimeout(refresh, 0); });

  refresh();
})();
