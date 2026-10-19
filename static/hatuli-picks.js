/* Progressive enhancement. Selection and source links work without JavaScript. */
(function () {
  'use strict';
  var page = document.querySelector('.picks-page');
  if (!page) return;
  var cards = Array.from(page.querySelectorAll('[data-picks-card]'));
  var filters = page.querySelector('[data-picks-filters]');
  var count = page.querySelector('[data-picks-count]');
  var empty = page.querySelector('[data-picks-filter-empty]');
  function selectRooms(value) {
    var visible = 0;
    cards.forEach(function (card) {
      var rooms = Number(card.dataset.rooms);
      var show = value === 'all' || (value === '4' ? rooms >= 4 : rooms === Number(value));
      card.hidden = !show;
      if (show) visible += 1;
    });
    filters.querySelectorAll('[data-picks-filter]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.picksFilter === value));
    });
    if (count) count.textContent = visible + ' из ' + cards.length + ' вариантов';
    if (empty) empty.hidden = visible !== 0;
  }
  if (filters && cards.length) {
    filters.hidden = false;
    filters.querySelectorAll('[data-picks-filter]').forEach(function (button) {
      button.addEventListener('click', function () { selectRooms(button.dataset.picksFilter); });
    });
    var reset = page.querySelector('[data-picks-reset]');
    if (reset) reset.addEventListener('click', function () {
      selectRooms('all');
      filters.querySelector('[data-picks-filter="all"]').focus();
    });
  }
  page.querySelectorAll('[data-picks-gallery]').forEach(function (gallery) {
    var image = gallery.querySelector('[data-picks-image]');
    if (!image) return;
    var photos;
    try { photos = JSON.parse(gallery.dataset.photos); } catch (_) { return; }
    if (!Array.isArray(photos) || !photos.length) return;
    var placeholder = gallery.querySelector('[data-picks-placeholder]');
    var controls = gallery.querySelector('[data-picks-photo-controls]');
    var counter = gallery.querySelector('[data-picks-photo-counter]');
    var index = 0;
    var failed = new Set();
    function updateCounter() {
      if (counter) counter.textContent = (index + 1) + ' / ' + photos.length;
    }
    function showImage(nextIndex) {
      index = nextIndex;
      image.hidden = false;
      if (placeholder) placeholder.hidden = true;
      image.src = photos[index];
      updateCounter();
    }
    function move(step) {
      if (failed.size >= photos.length) return;
      var next = index;
      for (var attempts = 0; attempts < photos.length; attempts += 1) {
        next = (next + step + photos.length) % photos.length;
        if (!failed.has(next)) { showImage(next); return; }
      }
    }
    function onError() {
      failed.add(index);
      if (failed.size >= photos.length) {
        image.hidden = true;
        if (placeholder) placeholder.hidden = false;
        if (controls) controls.hidden = true;
        return;
      }
      move(1);
    }
    image.addEventListener('error', onError);
    image.addEventListener('load', function () {
      image.hidden = false;
      if (placeholder) placeholder.hidden = true;
    });
    if (photos.length > 1 && controls) {
      controls.hidden = false;
      gallery.querySelector('[data-picks-photo-prev]').addEventListener('click', function () { move(-1); });
      gallery.querySelector('[data-picks-photo-next]').addEventListener('click', function () { move(1); });
    }
    // The first URL may fail before this deferred script attaches its listener.
    if (image.complete && image.naturalWidth === 0) onError();
  });
})();
