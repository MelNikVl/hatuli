#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/build_complex_identity_review_tool.py — задача 2026-08-31,
"Complex Identity: human labeling + impact assessment", шаг 1: генерирует
самодостаточный HTML review-инструмент для top-100 candidate-пар
(scripts/build_complex_relation_review_examples.py должен быть запущен
первым — читает его вывод).

Read-only генератор — ничего не пишет в БД, только компонует HTML-файл
из template (ниже) + base64-embedded датасет. Инструмент открывается
локально в браузере (file://), полностью офлайн после генерации —
никаких сетевых запросов, вся разметка живёт в localStorage вкладки,
экспорт — обычное скачивание JSON-файла через Blob.

Для каждой из 100 пар инструмент показывает: id A/B, названия,
застройщика, дистанцию, адрес, год, listing/property counts, примеры
объявлений (ссылки на krisha.kz), подсказку классификатора
(candidate_relation — ПОМЕЧЕНА как гипотеза, не ответ) и 6 label-кнопок
(duplicate_same_complex/renamed_same_complex/sibling_phase/
same_umbrella_project/separate_neighbor_complex/ambiguous). Top-30 по
impact (conflict_listing_count, затем properties) — отдельная вкладка,
это те же первые 30 записей top_100 (уже отсортированы build_complex_
relation_review_dataset.py в этом порядке — здесь НЕ пересчитывается).

Экспорт формирует relations_for_import — строки, совместимые с
complex_relations (migrations/095): canonical order, relation_type,
confidence, evidence, reviewed_by/reviewed_at, methodology_version.
`ambiguous` НЕ попадает в relations_for_import (уходит в отдельный
ambiguous_reviewed — review-статус, не факт для этой таблицы, тот же
принцип, что в самой миграции). Этот экспортированный файл — вход для
scripts/import_reviewed_complex_relations.py (validate-only) и
scripts/compute_complex_identity_review_impact.py (impact assessment).

    venv/bin/python scripts/build_complex_identity_review_tool.py
    # затем открыть в браузере:
    #   complex_identity_review_tool.html
"""
from __future__ import annotations

import base64
import json
import os

_IN_PATH = os.path.join(os.path.dirname(__file__), "..", "complex_relation_review_top100_enriched.json")
_OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "complex_identity_review_tool.html")

_HTML_TEMPLATE = r"""<meta charset="utf-8" />
<title>Complex Identity — разметка пар ЖК</title>
<style>
  :root {
    --paper: #EEF1F0;
    --paper-raised: #FFFFFF;
    --ink: #172127;
    --ink-dim: #4B5860;
    --ink-faint: #7C8891;
    --line: #C9D2D2;
    --line-soft: #DCE3E2;
    --accent: #1D5C7A;
    --accent-ink: #0D3A4E;
    --accent-soft: #DCEAEF;
    --accent-soft-line: #B9D4DE;

    --rel-duplicate: #A8412F;
    --rel-duplicate-soft: #F7E4DF;
    --rel-renamed: #9C6B15;
    --rel-renamed-soft: #F3E8D2;
    --rel-sibling: #2F7A57;
    --rel-sibling-soft: #DEEFE5;
    --rel-umbrella: #6C4E96;
    --rel-umbrella-soft: #E9E1F2;
    --rel-separate: #566573;
    --rel-separate-soft: #E4E8EB;
    --rel-ambiguous: #8A7A55;
    --rel-ambiguous-soft: #EFE9D8;

    --good: #2F7A57;
    --shadow: 0 1px 2px rgba(23,33,39,0.06), 0 4px 14px rgba(23,33,39,0.05);
    --radius: 3px;
    --mono: ui-monospace, "SF Mono", "Cascadia Mono", "JetBrains Mono", Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #101A20;
      --paper-raised: #16232B;
      --ink: #DDE6E9;
      --ink-dim: #A6B4BA;
      --ink-faint: #71818A;
      --line: #2B3B44;
      --line-soft: #223039;
      --accent: #5FA9C9;
      --accent-ink: #BFE2F0;
      --accent-soft: #17323D;
      --accent-soft-line: #275163;

      --rel-duplicate: #E08469;
      --rel-duplicate-soft: #3A241E;
      --rel-renamed: #D9AF54;
      --rel-renamed-soft: #392E17;
      --rel-sibling: #6FC79B;
      --rel-sibling-soft: #1B3327;
      --rel-umbrella: #B79BE0;
      --rel-umbrella-soft: #2C2440;
      --rel-separate: #A7B4BE;
      --rel-separate-soft: #26323A;
      --rel-ambiguous: #D2BF8C;
      --rel-ambiguous-soft: #362F1F;

      --good: #6FC79B;
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 6px 20px rgba(0,0,0,0.35);
    }
  }
  :root[data-theme="dark"] {
    --paper: #101A20; --paper-raised: #16232B; --ink: #DDE6E9; --ink-dim: #A6B4BA; --ink-faint: #71818A;
    --line: #2B3B44; --line-soft: #223039; --accent: #5FA9C9; --accent-ink: #BFE2F0;
    --accent-soft: #17323D; --accent-soft-line: #275163;
    --rel-duplicate: #E08469; --rel-duplicate-soft: #3A241E;
    --rel-renamed: #D9AF54; --rel-renamed-soft: #392E17;
    --rel-sibling: #6FC79B; --rel-sibling-soft: #1B3327;
    --rel-umbrella: #B79BE0; --rel-umbrella-soft: #2C2440;
    --rel-separate: #A7B4BE; --rel-separate-soft: #26323A;
    --rel-ambiguous: #D2BF8C; --rel-ambiguous-soft: #362F1F;
    --good: #6FC79B; --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 6px 20px rgba(0,0,0,0.35);
  }
  :root[data-theme="light"] {
    --paper: #EEF1F0; --paper-raised: #FFFFFF; --ink: #172127; --ink-dim: #4B5860; --ink-faint: #7C8891;
    --line: #C9D2D2; --line-soft: #DCE3E2; --accent: #1D5C7A; --accent-ink: #0D3A4E;
    --accent-soft: #DCEAEF; --accent-soft-line: #B9D4DE;
    --rel-duplicate: #A8412F; --rel-duplicate-soft: #F7E4DF;
    --rel-renamed: #9C6B15; --rel-renamed-soft: #F3E8D2;
    --rel-sibling: #2F7A57; --rel-sibling-soft: #DEEFE5;
    --rel-umbrella: #6C4E96; --rel-umbrella-soft: #E9E1F2;
    --rel-separate: #566573; --rel-separate-soft: #E4E8EB;
    --rel-ambiguous: #8A7A55; --rel-ambiguous-soft: #EFE9D8;
    --good: #2F7A57; --shadow: 0 1px 2px rgba(23,33,39,0.06), 0 4px 14px rgba(23,33,39,0.05);
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--paper);
    color: var(--ink);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.5;
  }
  ::selection { background: var(--accent-soft); }
  button, input, textarea { font-family: inherit; color: inherit; }
  a { color: var(--accent-ink); }

  .app { display: grid; grid-template-columns: 300px 1fr; min-height: 100vh; align-items: start; }
  @media (max-width: 900px) { .app { grid-template-columns: 1fr; } }

  /* ── rail ── */
  .rail {
    position: sticky; top: 0; align-self: start;
    height: 100vh; overflow-y: auto;
    border-right: 1px solid var(--line);
    padding: 22px 18px 24px;
    display: flex; flex-direction: column; gap: 20px;
    background: var(--paper);
  }
  @media (max-width: 900px) { .rail { position: static; height: auto; border-right: none; border-bottom: 1px solid var(--line); } }

  .eyebrow {
    font-size: 10.5px; letter-spacing: 0.09em; text-transform: uppercase;
    color: var(--accent); font-weight: 600;
  }
  .railhead h1 { font-size: 19px; margin: 3px 0 4px; font-weight: 700; text-wrap: balance; letter-spacing: -0.01em; }
  .railhead p { margin: 0; color: var(--ink-dim); font-size: 12.5px; }

  .field { display: flex; flex-direction: column; gap: 5px; }
  .field label { font-size: 11px; color: var(--ink-dim); text-transform: uppercase; letter-spacing: 0.04em; }
  .field input[type="text"] {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 7px 9px; font-size: 13px; color: var(--ink);
  }
  .field input[type="text"]:focus, textarea:focus, button:focus-visible, .chip-btn:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 1px;
  }

  .progress-block { display: flex; flex-direction: column; gap: 8px; }
  .progress-num { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 26px; font-weight: 600; letter-spacing: -0.02em; }
  .progress-num small { font-family: var(--sans); font-size: 12px; font-weight: 500; color: var(--ink-faint); }
  .progress-bar { height: 5px; border-radius: 3px; background: var(--line-soft); overflow: hidden; }
  .progress-bar > i { display: block; height: 100%; background: var(--accent); transition: width .25s ease; }

  .relcounts { display: flex; flex-direction: column; gap: 4px; }
  .relcount-row { display: flex; align-items: center; gap: 7px; font-size: 12px; }
  .relcount-row .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
  .relcount-row .label { flex: 1; color: var(--ink-dim); }
  .relcount-row .n { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink); }

  .segmented { display: flex; flex-direction: column; gap: 3px; border: 1px solid var(--line); border-radius: var(--radius); padding: 3px; background: var(--paper-raised); }
  .segmented button {
    border: none; background: transparent; text-align: left; padding: 7px 9px; border-radius: 2px;
    font-size: 12.5px; color: var(--ink-dim); cursor: pointer; display: flex; justify-content: space-between; gap: 8px;
  }
  .segmented button .n { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink-faint); }
  .segmented button.active { background: var(--accent-soft); color: var(--accent-ink); font-weight: 600; }
  .segmented button.active .n { color: var(--accent-ink); }
  .segmented button:hover:not(.active) { background: var(--line-soft); }

  .rail-actions { display: flex; flex-direction: column; gap: 7px; margin-top: auto; padding-top: 14px; border-top: 1px solid var(--line); }
  .btn {
    border: 1px solid var(--line); background: var(--paper-raised); color: var(--ink);
    padding: 8px 11px; border-radius: var(--radius); font-size: 12.5px; font-weight: 600; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 6px;
  }
  .btn:hover { border-color: var(--accent-soft-line); background: var(--accent-soft); }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #F4FAFC; }
  .btn.primary:hover { background: var(--accent-ink); border-color: var(--accent-ink); }
  .btn.danger:hover { border-color: var(--rel-duplicate); color: var(--rel-duplicate); background: var(--rel-duplicate-soft); }
  .btn.small { padding: 5px 9px; font-size: 11.5px; }
  .visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }

  .rail-note { font-size: 11px; color: var(--ink-faint); line-height: 1.55; }
  .rail-note code { font-family: var(--mono); background: var(--line-soft); padding: 1px 4px; border-radius: 2px; font-size: 10.5px; }

  /* ── main ── */
  .main { padding: 22px 26px 80px; max-width: 980px; }
  .main-topbar { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .main-topbar h2 { font-size: 15px; margin: 0; font-weight: 700; }
  .search { display: flex; align-items: center; gap: 6px; }
  .search input {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 6px 9px; font-size: 12.5px; width: 200px; color: var(--ink);
  }

  .cards { display: flex; flex-direction: column; gap: 14px; }
  .empty-state { color: var(--ink-faint); font-size: 13px; padding: 40px 0; text-align: center; }

  .card {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: 4px;
    box-shadow: var(--shadow); overflow: hidden;
  }
  .card.is-reviewed { border-left: 3px solid var(--card-rel-color, var(--accent)); }

  .card-head {
    display: flex; align-items: center; gap: 10px; padding: 11px 16px;
    border-bottom: 1px solid var(--line-soft); flex-wrap: wrap;
  }
  .rank-badge {
    font-family: var(--mono); font-size: 10.5px; font-weight: 600; color: var(--ink-faint);
    background: var(--line-soft); padding: 2px 6px; border-radius: 2px; flex: none;
  }
  .pair-ids { font-family: var(--mono); font-size: 12px; color: var(--ink-dim); font-variant-numeric: tabular-nums; }
  .impact-chip { font-size: 11px; color: var(--ink-faint); margin-left: auto; }
  .impact-chip b { color: var(--ink-dim); font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .status-chip {
    font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
    padding: 3px 8px; border-radius: 20px; white-space: nowrap;
  }
  .status-chip.pending { background: var(--line-soft); color: var(--ink-faint); }

  .compare {
    display: grid; grid-template-columns: 108px 1fr 1fr; gap: 0 12px;
    padding: 13px 16px 6px;
  }
  .compare .rowlabel { font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.03em; padding: 5px 0; align-self: start; }
  .compare .cell { font-size: 13px; padding: 5px 0; word-break: break-word; }
  .compare .cell.match { color: var(--good); }
  .compare .name-cell { font-weight: 600; font-size: 13.5px; }
  .compare-head { display: contents; }
  .compare-head .cell { font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.03em; padding-bottom: 3px; }

  .evidence-strip { display: flex; flex-wrap: wrap; gap: 6px; padding: 4px 16px 13px; }
  .ev-chip {
    font-size: 11px; font-family: var(--mono); color: var(--ink-dim); background: var(--line-soft);
    padding: 2.5px 7px; border-radius: 2px; font-variant-numeric: tabular-nums;
  }
  .ev-chip.yes { background: var(--accent-soft); color: var(--accent-ink); }

  .suggestion {
    margin: 0 16px 13px; padding: 7px 10px; border: 1px dashed var(--line); border-radius: 3px;
    font-size: 11.5px; color: var(--ink-dim); display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap;
  }
  .suggestion b { color: var(--ink); font-weight: 700; }
  .suggestion .hint { color: var(--ink-faint); font-size: 10.5px; }

  .examples { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; padding: 0 16px 13px; }
  .examples h4 { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--ink-faint); margin: 0 0 6px; font-weight: 600; }
  .ex-list { display: flex; flex-direction: column; gap: 4px; }
  .ex-item {
    display: flex; align-items: baseline; gap: 6px; font-size: 12px; text-decoration: none;
    color: var(--ink-dim); padding: 3px 6px; border-radius: 2px; border: 1px solid transparent;
  }
  .ex-item:hover { background: var(--paper); border-color: var(--line-soft); }
  .ex-dot { width: 6px; height: 6px; border-radius: 50%; flex: none; background: var(--ink-faint); }
  .ex-dot.active { background: var(--good); }
  .ex-item .price { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink); white-space: nowrap; }
  .ex-item .title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ex-none { font-size: 11.5px; color: var(--ink-faint); font-style: italic; padding: 3px 6px; }

  .verdict {
    border-top: 1px solid var(--line-soft); background: var(--paper);
    padding: 13px 16px 15px; display: flex; flex-direction: column; gap: 10px;
  }
  .relation-picker { display: flex; flex-wrap: wrap; gap: 6px; }
  .rel-btn {
    border: 1px solid var(--line); background: var(--paper-raised); border-radius: 20px;
    padding: 5px 12px; font-size: 12px; font-weight: 600; cursor: pointer; color: var(--ink-dim);
    display: flex; align-items: center; gap: 6px;
  }
  .rel-btn .dot { width: 7px; height: 7px; border-radius: 50%; }
  .rel-btn:hover { border-color: var(--rel-color); }
  .rel-btn.selected { background: var(--rel-soft); border-color: var(--rel-color); color: var(--rel-color); }

  .verdict-detail { display: none; align-items: center; gap: 14px; flex-wrap: wrap; }
  .verdict-detail.shown { display: flex; }
  .conf-field { display: flex; align-items: center; gap: 8px; }
  .conf-field label { font-size: 11px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.03em; }
  .conf-field input[type="range"] { width: 110px; accent-color: var(--accent); }
  .conf-val { font-family: var(--mono); font-size: 12.5px; font-variant-numeric: tabular-nums; min-width: 28px; }
  .notes-field { flex: 1 1 220px; }
  .notes-field textarea {
    width: 100%; min-height: 34px; resize: vertical; background: var(--paper-raised);
    border: 1px solid var(--line); border-radius: var(--radius); padding: 6px 8px; font-size: 12px;
  }
  .clear-link { font-size: 11px; color: var(--ink-faint); background: none; border: none; cursor: pointer; text-decoration: underline; padding: 0; }
  .clear-link:hover { color: var(--rel-duplicate); }
  .reviewed-meta { font-size: 10.5px; color: var(--ink-faint); font-family: var(--mono); }

  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<div class="app">
  <aside class="rail">
    <div class="railhead">
      <div class="eyebrow">Complex Identity · human review</div>
      <h1>Разметка пар ЖК</h1>
      <p>top-100 candidate-пар · без production writes</p>
    </div>

    <div class="field">
      <label for="reviewer-input">Кто размечает</label>
      <input type="text" id="reviewer-input" placeholder="имя / логин" autocomplete="off" />
    </div>

    <div class="progress-block">
      <div class="progress-num"><span id="progress-count">0</span><small> / <span id="progress-total">100</span> размечено</small></div>
      <div class="progress-bar"><i id="progress-fill" style="width:0%"></i></div>
      <div class="relcounts" id="relcounts"></div>
    </div>

    <nav class="segmented" id="view-tabs" aria-label="Фильтр списка">
      <button data-view="top30" class="active">Top-30 · impact <span class="n" id="count-top30">30</span></button>
      <button data-view="all">Все 100 <span class="n" id="count-all">100</span></button>
      <button data-view="reviewed">Размечено <span class="n" id="count-reviewed">0</span></button>
      <button data-view="unreviewed">Не размечено <span class="n" id="count-unreviewed">100</span></button>
    </nav>

    <div class="rail-actions">
      <button class="btn primary" id="export-btn">Экспортировать JSON</button>
      <button class="btn" id="import-btn">Импортировать прогресс</button>
      <input type="file" id="import-file" accept="application/json" class="visually-hidden" />
      <button class="btn danger" id="reset-btn">Сбросить всё</button>
      <p class="rail-note">
        Экспорт формирует <code>relations_for_import</code> — строки в формате <code>complex_relations</code>
        (canonical order, confidence, evidence, reviewed_by/at). <code>ambiguous</code> в эту таблицу не пишется —
        это review-статус, не факт. Ничего не отправляется в БД с этой страницы; импорт делает отдельный скрипт
        после ручной проверки экспортированного файла.
      </p>
    </div>
  </aside>

  <main class="main">
    <div class="main-topbar">
      <h2 id="list-title">Top-30 по impact (conflict_listing_count → properties)</h2>
      <div class="search">
        <input type="text" id="search-input" placeholder="id или название…" />
      </div>
    </div>
    <div class="cards" id="cards"></div>
    <div class="empty-state" id="empty-state" style="display:none;">Ничего не найдено под текущий фильтр.</div>
  </main>
</div>

<script>
(function () {
  "use strict";

  var DATA_B64 = "__DATA_B64__";

  function b64ToUtf8(b64) {
    var binary = atob(b64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder("utf-8").decode(bytes);
  }

  var DATASET = JSON.parse(b64ToUtf8(DATA_B64));
  var PAIRS = DATASET.top_100.map(function (r, i) { r._rank = i + 1; r._key = r.complex_id_a + "_" + r.complex_id_b; return r; });
  var TOP30_KEYS = {};
  PAIRS.slice(0, 30).forEach(function (r) { TOP30_KEYS[r._key] = true; });

  var RELATIONS = [
    { id: "duplicate_same_complex", label: "Дубликат", color: "var(--rel-duplicate)", soft: "var(--rel-duplicate-soft)" },
    { id: "renamed_same_complex", label: "Переименован", color: "var(--rel-renamed)", soft: "var(--rel-renamed-soft)" },
    { id: "sibling_phase", label: "Фаза/очередь", color: "var(--rel-sibling)", soft: "var(--rel-sibling-soft)" },
    { id: "same_umbrella_project", label: "Umbrella-проект", color: "var(--rel-umbrella)", soft: "var(--rel-umbrella-soft)" },
    { id: "separate_neighbor_complex", label: "Соседний, другой", color: "var(--rel-separate)", soft: "var(--rel-separate-soft)" },
    { id: "ambiguous", label: "Неоднозначно", color: "var(--rel-ambiguous)", soft: "var(--rel-ambiguous-soft)" }
  ];
  var REL_BY_ID = {};
  RELATIONS.forEach(function (r) { REL_BY_ID[r.id] = r; });

  var STORAGE_KEY = "complex-identity-review-v1";

  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) {}
    return { reviewer: "", cards: {} };
  }
  function saveState() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) {}
  }

  var state = loadState();
  var currentView = "top30";
  var searchQuery = "";

  var reviewerInput = document.getElementById("reviewer-input");
  reviewerInput.value = state.reviewer || "";
  reviewerInput.addEventListener("input", function () {
    state.reviewer = reviewerInput.value;
    saveState();
  });

  function fmtMoney(n) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString("ru-RU") + " ₸";
  }
  function fmtDist(m) {
    if (m === null || m === undefined) return "—";
    return Math.round(m).toLocaleString("ru-RU") + " м";
  }
  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmtDate(iso) {
    if (!iso) return "";
    try { return new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }); }
    catch (e) { return iso; }
  }

  function compareRow(label, a, b, opts) {
    opts = opts || {};
    var equal = a !== null && a !== undefined && a === b;
    var cls = "cell" + (equal && opts.highlightMatch !== false ? " match" : "") + (opts.nameCell ? " name-cell" : "");
    return (
      '<div class="rowlabel">' + esc(label) + "</div>" +
      '<div class="' + cls + '">' + (opts.raw ? opts.raw(a) : esc(a === null || a === undefined ? "—" : a)) + "</div>" +
      '<div class="' + cls + '">' + (opts.raw ? opts.raw(b) : esc(b === null || b === undefined ? "—" : b)) + "</div>"
    );
  }

  function exampleItem(ex) {
    return (
      '<a class="ex-item" href="' + esc(ex.url) + '" target="_blank" rel="noopener noreferrer" title="' + esc(ex.title) + '">' +
      '<span class="ex-dot' + (ex.is_active ? " active" : "") + '"></span>' +
      '<span class="title">' + esc(ex.title || ex.listing_id) + "</span>" +
      '<span class="price">' + fmtMoney(ex.price) + "</span>" +
      "</a>"
    );
  }

  function evChip(text, on) {
    return '<span class="ev-chip' + (on ? " yes" : "") + '">' + esc(text) + "</span>";
  }

  function renderCard(r) {
    var review = state.cards[r._key];
    var relMeta = review ? REL_BY_ID[review.relation_type] : null;
    var devA = r.developer_name_a || (r.developer_id_a !== null ? "developer_id " + r.developer_id_a : "—");
    var devB = r.developer_name_b || (r.developer_id_b !== null ? "developer_id " + r.developer_id_b : "—");
    var ev = r.evidence_summary || {};

    var head =
      '<div class="card-head">' +
      '<span class="rank-badge">#' + r._rank + "</span>" +
      '<span class="pair-ids">' + r.complex_id_a + " ↔ " + r.complex_id_b + "</span>" +
      '<span class="impact-chip">conflict listings <b>' + r.conflict_listing_count + "</b> · properties <b>" +
        (r.property_count_a + r.property_count_b) + "</b></span>" +
      (relMeta
        ? '<span class="status-chip" style="background:' + relMeta.soft + ";color:" + relMeta.color + '">' + relMeta.label + "</span>"
        : '<span class="status-chip pending">не размечено</span>') +
      "</div>";

    var compare =
      '<div class="compare">' +
      '<div class="compare-head"><div></div><div class="cell">A · ' + r.complex_id_a + '</div><div class="cell">B · ' + r.complex_id_b + "</div></div>" +
      compareRow("Название", r.name_a, r.name_b, { nameCell: true, highlightMatch: false }) +
      compareRow("Застройщик", devA, devB) +
      compareRow("Адрес", r.address_a, r.address_b) +
      compareRow("Год", r.year_built_a, r.year_built_b) +
      compareRow("Объявления", r.listing_count_a, r.listing_count_b, { highlightMatch: false }) +
      compareRow("Properties", r.property_count_a, r.property_count_b, { highlightMatch: false }) +
      "</div>";

    var evidence =
      '<div class="evidence-strip">' +
      evChip("dist " + fmtDist(r.distance_m), r.distance_m !== null && r.distance_m <= 60) +
      evChip("name_sim " + (ev.name_similarity !== undefined ? ev.name_similarity.toFixed(2) : "—"), ev.name_similarity >= 0.85) +
      evChip("same_developer", !!ev.same_developer) +
      evChip("same_street" + (ev.street_a ? " (" + ev.street_a + ")" : ""), !!ev.same_street) +
      evChip("root_match", !!ev.root_match) +
      evChip("shared_properties " + r.shared_property_count, r.shared_property_count > 0) +
      evChip("year_diff " + (ev.year_diff === null || ev.year_diff === undefined ? "—" : ev.year_diff), ev.year_diff !== null && ev.year_diff <= 1) +
      evChip("source: " + (r.sources || []).join("+"), false) +
      "</div>";

    var suggestion =
      '<div class="suggestion"><span>Классификатор предполагает:</span> <b>' +
      esc((REL_BY_ID[r.candidate_relation] || { label: r.candidate_relation }).label || r.candidate_relation) +
      '</b><span class="hint">— гипотеза скрипта build_complex_relation_review_dataset.py, не ответ. Финальную метку ставит человек.</span></div>';

    var exA = (r.examples_a || []).length ? '<div class="ex-list">' + r.examples_a.map(exampleItem).join("") + "</div>" : '<div class="ex-none">объявлений не найдено</div>';
    var exB = (r.examples_b || []).length ? '<div class="ex-list">' + r.examples_b.map(exampleItem).join("") + "</div>" : '<div class="ex-none">объявлений не найдено</div>';
    var examples =
      '<div class="examples">' +
      "<div><h4>Примеры · A</h4>" + exA + "</div>" +
      "<div><h4>Примеры · B</h4>" + exB + "</div>" +
      "</div>";

    var relBtns = RELATIONS.map(function (rel) {
      var selected = review && review.relation_type === rel.id;
      return (
        '<button type="button" class="rel-btn' + (selected ? " selected" : "") + '" data-rel="' + rel.id + '" data-key="' + r._key + '" ' +
        'style="--rel-color:' + rel.color + ";--rel-soft:" + rel.soft + '">' +
        '<span class="dot" style="background:' + rel.color + '"></span>' + rel.label +
        "</button>"
      );
    }).join("");

    var conf = review ? review.confidence : 0.8;
    var notes = review ? review.notes || "" : "";
    var metaLine = review ? "reviewed_by " + esc(review.reviewed_by) + " · " + fmtDate(review.reviewed_at) : "";

    var verdict =
      '<div class="verdict" data-key="' + r._key + '">' +
      '<div class="relation-picker">' + relBtns + "</div>" +
      '<div class="verdict-detail' + (review ? " shown" : "") + '">' +
      '<div class="conf-field"><label>Уверенность</label><input type="range" min="0" max="1" step="0.05" value="' + conf + '" class="conf-slider" data-key="' + r._key + '" /><span class="conf-val">' + conf.toFixed(2) + "</span></div>" +
      '<div class="notes-field"><textarea class="notes-input" data-key="' + r._key + '" placeholder="заметка / evidence override (необязательно)">' + esc(notes) + "</textarea></div>" +
      '<button type="button" class="clear-link" data-key="' + r._key + '">очистить</button>' +
      "</div>" +
      '<div class="reviewed-meta">' + metaLine + "</div>" +
      "</div>";

    var card = document.createElement("div");
    card.className = "card" + (review ? " is-reviewed" : "");
    if (relMeta) card.style.setProperty("--card-rel-color", relMeta.color);
    card.innerHTML = head + compare + evidence + suggestion + examples + verdict;
    return card;
  }

  function matchesSearch(r, q) {
    if (!q) return true;
    q = q.toLowerCase();
    return (
      String(r.complex_id_a).indexOf(q) !== -1 ||
      String(r.complex_id_b).indexOf(q) !== -1 ||
      (r.name_a || "").toLowerCase().indexOf(q) !== -1 ||
      (r.name_b || "").toLowerCase().indexOf(q) !== -1
    );
  }

  function currentList() {
    var list = PAIRS.filter(function (r) { return matchesSearch(r, searchQuery); });
    if (currentView === "top30") list = list.filter(function (r) { return TOP30_KEYS[r._key]; });
    else if (currentView === "reviewed") list = list.filter(function (r) { return !!state.cards[r._key]; });
    else if (currentView === "unreviewed") list = list.filter(function (r) { return !state.cards[r._key]; });
    return list;
  }

  var VIEW_TITLES = {
    top30: "Top-30 по impact (conflict_listing_count → properties)",
    all: "Все 100 candidate-пар",
    reviewed: "Размеченные пары",
    unreviewed: "Ещё не размечено"
  };

  function render() {
    var cardsEl = document.getElementById("cards");
    var list = currentList();
    cardsEl.innerHTML = "";
    var frag = document.createDocumentFragment();
    list.forEach(function (r) { frag.appendChild(renderCard(r)); });
    cardsEl.appendChild(frag);
    document.getElementById("empty-state").style.display = list.length ? "none" : "block";
    document.getElementById("list-title").textContent = VIEW_TITLES[currentView] + " (" + list.length + ")";
    renderStats();
  }

  function renderStats() {
    var reviewedKeys = Object.keys(state.cards);
    var reviewedCount = reviewedKeys.length;
    document.getElementById("progress-count").textContent = reviewedCount;
    document.getElementById("progress-fill").style.width = (reviewedCount) + "%";
    document.getElementById("count-top30").textContent = Object.keys(TOP30_KEYS).length;
    document.getElementById("count-all").textContent = PAIRS.length;
    document.getElementById("count-reviewed").textContent = reviewedCount;
    document.getElementById("count-unreviewed").textContent = PAIRS.length - reviewedCount;

    var counts = {};
    RELATIONS.forEach(function (rel) { counts[rel.id] = 0; });
    reviewedKeys.forEach(function (k) {
      var rt = state.cards[k].relation_type;
      if (counts[rt] !== undefined) counts[rt]++;
    });
    var relcountsEl = document.getElementById("relcounts");
    relcountsEl.innerHTML = RELATIONS.map(function (rel) {
      return (
        '<div class="relcount-row"><span class="dot" style="background:' + rel.color + '"></span>' +
        '<span class="label">' + rel.label + '</span><span class="n">' + counts[rel.id] + "</span></div>"
      );
    }).join("");
  }

  document.getElementById("view-tabs").addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-view]");
    if (!btn) return;
    currentView = btn.getAttribute("data-view");
    Array.prototype.forEach.call(document.querySelectorAll("#view-tabs button"), function (b) {
      b.classList.toggle("active", b === btn);
    });
    render();
  });

  document.getElementById("search-input").addEventListener("input", function (e) {
    searchQuery = e.target.value.trim();
    render();
  });

  document.getElementById("cards").addEventListener("click", function (e) {
    var relBtn = e.target.closest(".rel-btn");
    if (relBtn) {
      var key = relBtn.getAttribute("data-key");
      var relId = relBtn.getAttribute("data-rel");
      var existing = state.cards[key];
      state.cards[key] = {
        relation_type: relId,
        confidence: existing ? existing.confidence : 0.8,
        notes: existing ? existing.notes : "",
        reviewed_by: state.reviewer || "",
        reviewed_at: new Date().toISOString()
      };
      saveState();
      render();
      return;
    }
    var clearBtn = e.target.closest(".clear-link");
    if (clearBtn) {
      var ckey = clearBtn.getAttribute("data-key");
      delete state.cards[ckey];
      saveState();
      render();
      return;
    }
  });

  document.getElementById("cards").addEventListener("input", function (e) {
    if (e.target.classList.contains("conf-slider")) {
      var key = e.target.getAttribute("data-key");
      if (state.cards[key]) {
        state.cards[key].confidence = parseFloat(e.target.value);
        e.target.parentElement.querySelector(".conf-val").textContent = state.cards[key].confidence.toFixed(2);
        saveState();
      }
    }
    if (e.target.classList.contains("notes-input")) {
      var nkey = e.target.getAttribute("data-key");
      if (state.cards[nkey]) {
        state.cards[nkey].notes = e.target.value;
        saveState();
      }
    }
  });

  function buildExport() {
    var relationsForImport = [];
    var ambiguousReviewed = [];
    var unreviewedPairs = [];
    PAIRS.forEach(function (r) {
      var review = state.cards[r._key];
      if (!review) {
        unreviewedPairs.push({ complex_id_a: r.complex_id_a, complex_id_b: r.complex_id_b, name_a: r.name_a, name_b: r.name_b });
        return;
      }
      if (review.relation_type === "ambiguous") {
        ambiguousReviewed.push({
          complex_id_a: r.complex_id_a, complex_id_b: r.complex_id_b,
          reviewed_by: review.reviewed_by, reviewed_at: review.reviewed_at, notes: review.notes || null
        });
        return;
      }
      relationsForImport.push({
        complex_id_a: r.complex_id_a,
        complex_id_b: r.complex_id_b,
        relation_type: review.relation_type,
        confidence: review.confidence,
        evidence: {
          evidence_summary: r.evidence_summary,
          distance_m: r.distance_m,
          shared_property_count: r.shared_property_count,
          conflict_listing_count: r.conflict_listing_count,
          candidate_relation: r.candidate_relation,
          reviewer_notes: review.notes || null
        },
        reviewed_by: review.reviewed_by,
        reviewed_at: review.reviewed_at,
        methodology_version: "human_review_top100_v1"
      });
    });
    return {
      generated_from: "complex_relation_review_top100_enriched.json",
      exported_at: new Date().toISOString(),
      reviewer_default: state.reviewer || null,
      total_pairs: PAIRS.length,
      reviewed_count: Object.keys(state.cards).length,
      relations_for_import: relationsForImport,
      ambiguous_reviewed: ambiguousReviewed,
      unreviewed_pairs: unreviewedPairs
    };
  }

  document.getElementById("export-btn").addEventListener("click", function () {
    var payload = buildExport();
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    var stamp = new Date().toISOString().slice(0, 10);
    a.href = url;
    a.download = "complex_relations_reviewed_" + stamp + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  });

  document.getElementById("import-btn").addEventListener("click", function () {
    document.getElementById("import-file").click();
  });
  document.getElementById("import-file").addEventListener("change", function (e) {
    var file = e.target.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var payload = JSON.parse(reader.result);
        var restored = { reviewer: payload.reviewer_default || state.reviewer, cards: {} };
        (payload.relations_for_import || []).forEach(function (row) {
          var key = row.complex_id_a + "_" + row.complex_id_b;
          restored.cards[key] = {
            relation_type: row.relation_type,
            confidence: row.confidence,
            notes: row.evidence && row.evidence.reviewer_notes,
            reviewed_by: row.reviewed_by,
            reviewed_at: row.reviewed_at
          };
        });
        (payload.ambiguous_reviewed || []).forEach(function (row) {
          var key = row.complex_id_a + "_" + row.complex_id_b;
          restored.cards[key] = {
            relation_type: "ambiguous",
            confidence: 0.5,
            notes: row.notes,
            reviewed_by: row.reviewed_by,
            reviewed_at: row.reviewed_at
          };
        });
        state = restored;
        reviewerInput.value = state.reviewer || "";
        saveState();
        render();
      } catch (err) {
        alert("Не удалось прочитать файл: " + err.message);
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  });

  document.getElementById("reset-btn").addEventListener("click", function () {
    if (!confirm("Сбросить весь размеченный прогресс в этом браузере? Экспортированные файлы не пострадают.")) return;
    state = { reviewer: state.reviewer, cards: {} };
    saveState();
    render();
  });

  render();
})();
</script>
"""


def main() -> None:
    with open(_IN_PATH, encoding="utf-8") as f:
        dataset = json.load(f)
    raw = json.dumps(dataset, ensure_ascii=False).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")

    html = _HTML_TEMPLATE.replace("__DATA_B64__", b64)
    with open(_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"review tool written to {os.path.abspath(_OUT_PATH)} "
          f"({len(dataset.get('top_100', []))} pairs embedded, {len(html):,} bytes)")
    print("open it directly in a browser (file://) — fully offline, no DB/network calls.")
    print("NOT committed to git — local artifact, same as the JSON datasets it's built from.")


if __name__ == "__main__":
    main()
