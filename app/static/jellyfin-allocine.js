/*
 * Notes AlloCiné pour l'interface web de Jellyfin.
 *
 * Ajoute, dans la ligne d'informations de la page de détail d'un film ou d'une série
 * (année, durée, classification, ★, 🍅…), deux notes AlloCiné sur 5, chacune avec son icône :
 *
 *   [journal] 4.5/5   → note de la PRESSE
 *   [personne] 4.3/5  → note des SPECTATEURS
 *
 * L'étoile et la tomate natives de Jellyfin (notes communautaires / Rotten Tomatoes) ne sont ni
 * modifiées ni masquées : ce badge s'ajoute à côté, avec ses propres icônes.
 *
 * Les données viennent de l'application « Movies Tracker » (GET /jellyfin/allocine-notes).
 *
 * INSTALLATION (une seule ligne à adapter) : dans le plugin « JavaScript Injector » de
 * Jellyfin, coller :
 *
 *   window.ALLOCINE_API_BASE = 'http://ADRESSE-DU-CONTENEUR:8095';
 *   var s = document.createElement('script');
 *   s.src = window.ALLOCINE_API_BASE + '/static/jellyfin-allocine.js';
 *   document.head.appendChild(s);
 *
 * Le script est volontairement défensif : s'il ne reconnaît pas la page, ou si l'application
 * est injoignable, il ne fait RIEN (un avertissement apparaît dans la console du navigateur).
 * Syntaxe volontairement conservatrice (pas de ?. ni de .finally) : certaines télés
 * (webOS, Tizen) embarquent des navigateurs anciens.
 */
(function () {
  'use strict';

  if (window.__allocineBadge) return; // déjà chargé (le plugin peut injecter deux fois)

  var TICK_MS = 1000;                 // Jellyfin est une SPA : on surveille la page chaque seconde
  var NOTES_TTL_MS = 10 * 60 * 1000;  // les notes changent rarement
  var RETRY_MS = 60 * 1000;           // après un échec, on ne réessaie pas à chaque seconde
  var STYLE_ID = 'allocine-badge-style';

  var script = document.currentScript;
  var base = window.ALLOCINE_API_BASE || (script && script.src ? new URL(script.src).origin : '');
  base = String(base || '').replace(/\/+$/, '');
  if (!base) {
    console.warn('[AlloCiné] adresse de l\'application inconnue : définissez window.ALLOCINE_API_BASE.');
    return;
  }

  // ---------- utilitaires purs ----------

  // Identifiant de l'item affiché : « #/details?id=<id>&serverId=… » (ancien format : « #!/details?id=… »).
  function currentItemId() {
    var hash = window.location.hash || '';
    if (hash.indexOf('details') === -1) return null;
    var q = hash.indexOf('?') >= 0 ? hash.slice(hash.indexOf('?') + 1) : '';
    var id = new URLSearchParams(q).get('id');
    return id ? id.replace(/-/g, '').toLowerCase() : null;
  }

  function fmt(n) {
    return Number(n).toFixed(1);
  }

  // Le lien vient de l'application ; on n'accepte pourtant que AlloCiné (jamais javascript:, etc.).
  function safeUrl(u) {
    return typeof u === 'string' && u.indexOf('https://www.allocine.fr/') === 0 ? u : null;
  }

  function hasNote(v) {
    return v !== null && v !== undefined && !isNaN(Number(v));
  }

  // ---------- notes (chargées une fois, gardées en mémoire) ----------

  var notes = null, notesAt = 0, inflight = null, failedAt = 0;

  function loadNotes() {
    var now = Date.now();
    if (notes && now - notesAt < NOTES_TTL_MS) return Promise.resolve(notes);
    if (inflight) return inflight;
    if (failedAt && now - failedAt < RETRY_MS) return Promise.resolve(notes);
    inflight = fetch(base + '/jellyfin/allocine-notes', { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (j) {
        notes = (j && j.items) || {};
        notesAt = Date.now();
        failedAt = 0;
        return notes;
      })
      .catch(function (e) {
        failedAt = Date.now();
        console.warn('[AlloCiné] notes indisponibles (' + e.message + ') depuis ' + base +
          '. Causes fréquentes : adresse incorrecte, ou page Jellyfin en https alors que l\'application est en http ' +
          '(contenu mixte), ou politique de sécurité (CSP) du proxy qui interdit d\'autres origines.');
        return notes;
      })
      .then(function (v) { inflight = null; return v; });
    return inflight;
  }

  // ---------- rendu ----------

  // Icônes en SVG « trait » (comme celles de Jellyfin) : elles prennent la couleur du texte
  // (currentColor) et la taille de la police, donc suivent le thème. Constantes uniquement : aucune
  // donnée venant du réseau n'est jamais insérée comme HTML.
  var SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
                 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">';
  var ICON_PRESSE = SVG_OPEN +
    '<path d="M4 4h12v15a1 1 0 0 1-1 1H6a2 2 0 0 1-2-2z"/>' +
    '<path d="M16 8h3a1 1 0 0 1 1 1v9a2 2 0 0 1-2 2h-3"/>' +
    '<path d="M7 8h6M7 12h6M7 16h3"/></svg>';
  var ICON_SPECTATEURS = SVG_OPEN +
    '<circle cx="12" cy="8" r="4"/>' +
    '<path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/></svg>';

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent =
      '.allocine-badge{display:inline-flex;align-items:center;margin-left:.9em;vertical-align:middle;white-space:nowrap}' +
      '.allocine-badge a{color:inherit;text-decoration:none;display:inline-flex;align-items:center}' +
      '.allocine-note{display:inline-flex;align-items:center;margin-right:.9em}' +
      '.allocine-note:last-child{margin-right:0}' +
      '.allocine-note svg{width:1.25em;height:1.25em;margin-right:.35em;flex:none}';
    document.head.appendChild(st);
  }

  // Une note : icône + « 4.5/5 ». Le texte reste lisible sans l'icône (lecteur d'écran, copier-coller).
  function addNote(parent, value, kind, icon, tip) {
    if (!hasNote(value)) return;
    var span = document.createElement('span');
    span.className = 'allocine-note allocine-' + kind;
    span.title = tip + ' AlloCiné : ' + fmt(value) + ' / 5';
    span.setAttribute('aria-label', tip + ' AlloCiné : ' + fmt(value) + ' sur 5');
    span.innerHTML = icon;
    span.appendChild(document.createTextNode(fmt(value) + '/5'));
    if (parent.childNodes.length) parent.appendChild(document.createTextNode(' '));
    parent.appendChild(span);
  }

  function buildBadge(itemId, n) {
    var root = document.createElement('div');
    root.className = 'mediaInfoItem allocine-badge';
    root.setAttribute('data-item-id', itemId);
    var url = safeUrl(n.u);
    var holder = document.createElement(url ? 'a' : 'span');
    if (url) {
      holder.href = url;
      holder.target = '_blank';
      holder.rel = 'noopener noreferrer';
      holder.title = 'Voir la fiche sur AlloCiné';
    }
    addNote(holder, n.p, 'presse', ICON_PRESSE, 'Note de la presse');
    addNote(holder, n.s, 'spectateurs', ICON_SPECTATEURS, 'Note des spectateurs');
    root.appendChild(holder);
    return root;
  }

  // Le badge se place juste après la tomate, sinon après l'étoile, sinon après la classification
  // d'âge ; à défaut, à la fin de la ligne.
  function insertBadge(box, badge) {
    var anchor = box.querySelector('.mediaInfoItem.mediaInfoCriticRating') ||
                 box.querySelector('.starRatingContainer.mediaInfoItem') ||
                 box.querySelector('.mediaInfoItem.mediaInfoText.mediaInfoOfficialRating');
    if (anchor && anchor.parentNode === box) {
      box.insertBefore(badge, anchor.nextSibling);
    } else {
      box.appendChild(badge);
    }
  }

  // ---------- boucle de surveillance ----------

  function tick() {
    var boxes = document.querySelectorAll('.itemMiscInfo.itemMiscInfo-primary');
    if (!boxes.length) return;

    var id = currentItemId();
    var todo = [];
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      var existing = box.querySelector('.allocine-badge');
      if (existing && id && existing.getAttribute('data-item-id') === id) continue; // déjà à jour
      if (existing) existing.parentNode.removeChild(existing);                       // autre item : on retire
      if (id) todo.push(box);
    }
    if (!todo.length) return;

    loadNotes().then(function (all) {
      // La page a pu changer pendant le chargement : on relit l'item courant.
      var nowId = currentItemId();
      var n = all && nowId ? all[nowId] : null;
      if (!n || (!hasNote(n.s) && !hasNote(n.p))) return; // pas de note AlloCiné pour cet item
      ensureStyle();
      for (var j = 0; j < todo.length; j++) {
        if (!document.body.contains(todo[j])) continue;
        if (todo[j].querySelector('.allocine-badge')) continue;
        insertBadge(todo[j], buildBadge(nowId, n));
      }
    });
  }

  window.__allocineBadge = { tick: tick, version: '2.0' };
  setInterval(tick, TICK_MS);
  tick();
  console.info('[AlloCiné] badge actif — notes lues depuis ' + base);
})();
