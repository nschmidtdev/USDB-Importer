(function () {
  'use strict';

  const SUPPORTED = ['en', 'de', 'es', 'ru'];
  const STORAGE_KEY = 'usi_language';
  let baseCatalog = {};
  let activeCatalog = {};
  let activeLanguage = 'en';
  let preference = 'auto';

  function normalizeLanguage(value) {
    const language = String(value || '').toLowerCase().split(/[-_]/)[0];
    return SUPPORTED.includes(language) ? language : 'en';
  }

  function systemLanguage() {
    const languages = Array.isArray(navigator.languages) && navigator.languages.length
      ? navigator.languages
      : [navigator.language || 'en'];
    for (const language of languages) {
      const normalized = normalizeLanguage(language);
      if (SUPPORTED.includes(normalized) && normalized !== 'en') return normalized;
      if (String(language).toLowerCase().startsWith('en')) return 'en';
    }
    return 'en';
  }

  function format(message, params) {
    return String(message).replace(/\{([a-zA-Z0-9_]+)\}/g, function (_, key) {
      return Object.prototype.hasOwnProperty.call(params || {}, key) ? String(params[key]) : '{' + key + '}';
    });
  }

  function t(key, params) {
    const message = activeCatalog[key] || baseCatalog[key] || key;
    return format(message, params || {});
  }

  async function fetchCatalog(language) {
    const response = await fetch('/static/i18n/' + language + '.json', {cache: 'no-cache'});
    if (!response.ok) throw new Error('Could not load language: ' + language);
    return response.json();
  }

  function apply(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(function (element) {
      element.textContent = t(element.dataset.i18n);
    });
    scope.querySelectorAll('[data-i18n-html]').forEach(function (element) {
      element.innerHTML = t(element.dataset.i18nHtml);
    });
    scope.querySelectorAll('[data-i18n-placeholder]').forEach(function (element) {
      element.setAttribute('placeholder', t(element.dataset.i18nPlaceholder));
    });
    scope.querySelectorAll('[data-i18n-title]').forEach(function (element) {
      element.setAttribute('title', t(element.dataset.i18nTitle));
    });
  }

  async function activate(language, emitChange) {
    const normalized = normalizeLanguage(language);
    if (!Object.keys(baseCatalog).length) baseCatalog = await fetchCatalog('en');
    activeCatalog = normalized === 'en' ? baseCatalog : await fetchCatalog(normalized);
    activeLanguage = normalized;
    document.documentElement.lang = normalized;
    document.title = t('app.title');
    apply(document);
    if (emitChange !== false) {
      document.dispatchEvent(new CustomEvent('usi:languagechange', {detail: {language: normalized}}));
    }
  }

  async function setPreference(value) {
    preference = value === 'auto' ? 'auto' : normalizeLanguage(value);
    localStorage.setItem(STORAGE_KEY, preference);
    const select = document.getElementById('languageSelect');
    if (select) select.disabled = true;
    try {
      await activate(preference === 'auto' ? systemLanguage() : preference);
      if (select) select.value = preference;
    } finally {
      if (select) select.disabled = false;
    }
  }

  async function init() {
    const saved = localStorage.getItem(STORAGE_KEY);
    preference = saved === 'auto' || SUPPORTED.includes(saved) ? saved : 'auto';
    const select = document.getElementById('languageSelect');
    if (select) {
      select.value = preference;
      select.addEventListener('change', function () {
        setPreference(this.value).catch(function (error) { console.error('language change failed', error); });
      });
    }
    await activate(preference === 'auto' ? systemLanguage() : preference, false);
    if (select) select.value = preference;
  }

  window.USII18n = {
    apply,
    init,
    normalizeLanguage,
    setPreference,
    t,
    get language() { return activeLanguage; },
    get preference() { return preference; },
  };
}());
