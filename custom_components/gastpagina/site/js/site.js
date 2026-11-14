/* ’t Zeemonster — website behaviour: the peeking monster, the photo viewer and the booking request. */
(function () {
  "use strict";

  /* --- The monster peeks only in the illustration closest to the middle of the screen --- */
  function spots() {
    var els = document.querySelectorAll("[data-spot]");
    var mid = window.innerHeight / 2, best = null, bestD = Infinity;
    for (var i = 0; i < els.length; i++) {
      var r = els[i].getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight || r.height === 0) continue;
      var d = Math.abs((r.top + r.bottom) / 2 - mid);
      if (d < bestD) { bestD = d; best = els[i].getAttribute("data-spot"); }
    }
    if (!best) return;
    var mv = document.querySelectorAll("[data-mv]");
    for (var j = 0; j < mv.length; j++) {
      mv[j].style.opacity = mv[j].getAttribute("data-mv") === best ? "1" : "0";
    }
  }
  var ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(function () { ticking = false; spots(); });
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);

  /* --- Photo viewer --- */
  var SETS = {
    gal: [
      { n: "05", src: "img/tafel-keuken.jpg", alt: "Keukenboog met de gedekte eettafel", cap: "Aan tafel, met de keuken erbij" },
      { n: "06", src: "img/kookeiland.jpg", alt: "Het eiken kookeiland onder de boog", cap: "Het kookeiland" },
      { n: "03", src: "img/zithoek.jpg", alt: "Leren bank, oranje fauteuil en een eiken salontafel", cap: "De zithoek, met de oranje fauteuil" },
      { n: "08", src: "img/tv-duin.jpg", alt: "Bank voor een tv met een duinbeeld", cap: "Het duin staat op de tv. Niet achter het raam." },
      { n: "02", src: "img/leefruimte.jpg", alt: "Overzicht van de leefruimte met eettafel en zithoek", cap: "De leefruimte in één keer" }
    ],
    slaap: [
      { n: "Slaapkamer · 1/3", src: "img/slaapkamer-1.jpg", alt: "Dubbel bed voor het golvende roze hoofdbord", cap: "Het golvende hoofdbord, met licht erachter" },
      { n: "Slaapkamer · 2/3", src: "img/slaapkamer-2.jpg", alt: "De slaapkamer met dubbel bed en ingebouwde kasten", cap: "Een dubbel bed. Voor twee." },
      { n: "Slaapkamer · 3/3", src: "img/slaapkamer-3.jpg", alt: "Kussens en dekbed van dichtbij", cap: "Kussens genoeg." }
    ],
    ruimte: [
      { n: "Slaapruimte · 1/3", src: "img/slaapruimte-1.jpg", alt: "Grenen stapelbed met geruit beddengoed", cap: "Een stapelbed. Boven of onder, jullie kiezen." },
      { n: "Slaapruimte · 2/3", src: "img/slaapruimte-2.jpg", alt: "Stapelbed naast de deur en een smalle binnenruit", cap: "Compact, met ventilatie." },
      { n: "Slaapruimte · 3/3", src: "img/slaapruimte-3.jpg", alt: "Stapelbed met de smalle binnenruit ernaast", cap: "Geen raam naar buiten." }
    ],
    bad: [
      { n: "Badkamer · 1/3", src: "img/badkamer-1.jpg", alt: "Waskom voor terracotta tegels en een verlichte ronde spiegel", cap: "De ronde spiegel" },
      { n: "Badkamer · 2/3", src: "img/badkamer-2.jpg", alt: "Badkamer met waskom en inloopdouche", cap: "Waskom en douche" },
      { n: "Badkamer · 3/3", src: "img/badkamer-3.jpg", alt: "Inloopdouche met terrazzotegels", cap: "De terrazzodouche" }
    ]
  };
  var viewer = document.querySelector(".zm-viewer");
  var current = { set: "gal", i: 0 }, opener = null;
  function part(name) { return viewer.querySelector('[data-viewer="' + name + '"]'); }
  function show() {
    var photo = SETS[current.set][current.i];
    part("n").textContent = photo.n;
    part("cap").textContent = photo.cap;
    part("img").src = photo.src;
    part("img").alt = photo.alt;
  }
  function open(set, i, from) {
    current = { set: set, i: i };
    opener = from;
    show();
    viewer.hidden = false;
    part("close").focus();
  }
  function close() {
    viewer.hidden = true;
    if (opener) opener.focus();
  }
  function step(delta) {
    var n = SETS[current.set].length;
    current.i = (current.i + delta + n) % n;
    show();
  }
  if (viewer) {
    document.querySelectorAll("[data-set]").forEach(function (button) {
      button.addEventListener("click", function () {
        open(button.getAttribute("data-set"), Number(button.getAttribute("data-photo")) || 0, button);
      });
    });
    part("close").addEventListener("click", close);
    part("prev").addEventListener("click", function () { step(-1); });
    part("next").addEventListener("click", function () { step(1); });
    document.addEventListener("keydown", function (e) {
      if (viewer.hidden) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
    });
  }

  /* --- Booking request --- */
  var API = "/api/gastpagina/";
  var form = document.getElementById("aanvraag");
  if (!form) { spots(); return; }
  var info = null, priced = false, sending = false;
  var submit = form.querySelector("[data-submit]");
  var msgDates = form.querySelector('[data-msg="datums"]');
  var msgForm = form.querySelector('[data-msg="form"]');
  var priceBox = form.querySelector("[data-price]");
  var contact = form.querySelector("[data-contact]");
  var ERRORS = {
    bezet: "Die nachten zijn helaas al bezet. Kies andere data.",
    te_kort: "Dat verblijf is te kort voor die periode.",
    personen: "Er kunnen hoogstens vier gasten logeren.",
    te_ver: "Zo ver vooruit kun je nog niet boeken.",
    verleden: "Die datum is al voorbij.",
    te_laat: "Zo kort op voorhand kunnen we niet meer bevestigen. Bel of mail ons even.",
    aankomstdag: "Op die dag kun je niet aankomen.",
    datums: "Kies een vertrek na je aankomst.",
    te_veel_aanvragen: "Er staat al een aanvraag van jou open, of er kwamen net te veel aanvragen binnen. Probeer het later opnieuw of mail ons.",
    niet_beschikbaar: "Boeken lukt nu even niet online. Probeer het later opnieuw of mail ons.",
    geen_prijs: "Voor die periode hebben we nog geen prijs. Stuur gerust een aanvraag."
  };
  var FIELDS = { naam: "Vul je naam in.", email: "Kijk je e-mailadres na.", telefoon: "Kijk je telefoonnummer na." };

  function say(el, text, kind) { el.textContent = text || ""; el.setAttribute("data-kind", kind || ""); }
  function euro(v) { return (v < 0 ? "− € " : "€ ") + Math.abs(Number(v)).toLocaleString("nl-BE", { minimumFractionDigits: 0, maximumFractionDigits: 2 }); }
  function value(name) { return (form.elements[name].value || "").trim(); }
  function nightsFree(a, b) {
    if (!info) return true;
    return !info.bezet.some(function (r) { return r[0] < b && a < r[1]; });
  }
  function setButton(text) { submit.firstChild.nodeValue = text + " "; }
  function reset() {
    priced = false;
    priceBox.hidden = true;
    contact.hidden = true;
    setButton("Beschikbaarheid bekijken");
    say(msgForm, "");
  }

  fetch(API + "beschikbaarheid", { headers: { Accept: "application/json" } })
    .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function (data) {
      info = data;
      form.elements.aankomst.min = data.van;
      form.elements.vertrek.min = data.van;
      form.elements.aankomst.max = data.tot;
      form.elements.vertrek.max = data.tot;
      var rules = "Inchecken vanaf " + data.inchecken + ", uitchecken tot " + data.uitchecken +
        ". Minimum " + data.min_nachten + " nachten";
      if (data.seizoenen && data.seizoenen.length) rules += ", in sommige periodes langer";
      document.querySelectorAll("[data-rules]").forEach(function (el) { el.textContent = rules + "."; });
    })
    .catch(function () { say(msgDates, ERRORS.niet_beschikbaar, "fout"); });

  ["aankomst", "vertrek", "personen"].forEach(function (name) {
    form.elements[name].addEventListener("change", function () {
      if (name === "aankomst" && value("aankomst")) {
        form.elements.vertrek.min = value("aankomst");
        if (value("vertrek") && value("vertrek") <= value("aankomst")) form.elements.vertrek.value = "";
      }
      reset();
      var a = value("aankomst"), b = value("vertrek");
      if (a && b && !nightsFree(a, b)) say(msgDates, ERRORS.bezet, "fout");
      else say(msgDates, "");
    });
  });

  function checkDates() {
    var a = value("aankomst"), b = value("vertrek");
    if (!a || !b) { say(msgDates, "Kies je aankomst en vertrek.", "fout"); return false; }
    if (b <= a) { say(msgDates, ERRORS.datums, "fout"); return false; }
    if (!nightsFree(a, b)) { say(msgDates, ERRORS.bezet, "fout"); return false; }
    return true;
  }

  function showPrice() {
    var q = "aankomst=" + value("aankomst") + "&vertrek=" + value("vertrek") + "&personen=" + value("personen");
    submit.disabled = true;
    return fetch(API + "prijs?" + q, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (body) { return { ok: r.ok, body: body }; }); })
      .then(function (res) {
        if (!res.ok && res.body.error !== "geen_prijs") { say(msgDates, ERRORS[res.body.error] || ERRORS.niet_beschikbaar, "fout"); return; }
        say(msgDates, "Vrij. Vul je gegevens in en stuur je aanvraag.", "ok");
        priceBox.textContent = "";
        if (res.ok) {
          var p = res.body, dl = document.createElement("dl");
          [["Verblijf, " + p.nachten + (p.nachten === 1 ? " nacht" : " nachten"), p.logies],
           ["Extra gasten", p.extra_gasten],
           ["Weekkorting", p.weekkorting ? -p.weekkorting : 0],
           ["Schoonmaak", p.schoonmaak],
           ["Toeristenbelasting", p.toeristenbelasting]].forEach(function (row) {
            if (!row[1] && row[0].indexOf("Verblijf") !== 0) return; // all-in: no empty rows
            var dt = document.createElement("dt"), dd = document.createElement("dd");
            dt.textContent = row[0]; dd.textContent = euro(row[1]); dl.appendChild(dt); dl.appendChild(dd);
          });
          var dt = document.createElement("dt"), dd = document.createElement("dd");
          dt.textContent = "Totaal (indicatief)"; dd.textContent = euro(p.totaal);
          dt.className = dd.className = "zm-total"; dl.appendChild(dt); dl.appendChild(dd);
          priceBox.appendChild(dl);
          priceBox.hidden = false;
        }
        contact.hidden = false;
        priced = true;
        setButton("Aanvraag versturen");
        form.elements.naam.focus();
      })
      .catch(function () { say(msgDates, ERRORS.niet_beschikbaar, "fout"); })
      .then(function () { submit.disabled = false; });
  }

  function send() {
    var missing = ["naam", "email", "telefoon"].filter(function (n) { return !form.elements[n].checkValidity() || !value(n); });
    if (missing.length) { say(msgForm, FIELDS[missing[0]], "fout"); form.elements[missing[0]].focus(); return; }
    sending = true;
    submit.disabled = true;
    say(msgForm, "Even geduld…");
    var body = {
      naam: value("naam"), email: value("email"), telefoon: value("telefoon"),
      personen: Number(value("personen")), aankomst: value("aankomst"), vertrek: value("vertrek"),
      bericht: form.elements.bericht.value, website: form.elements.website.value
    };
    fetch(API + "aanvraag", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status === 201) {
          form.innerHTML = "";
          var h = document.createElement("p"), p = document.createElement("p");
          h.textContent = "Dank je, " + body.naam.split(" ")[0] + ".";
          h.style.cssText = "margin:0;font-family:Fraunces,Georgia,serif;font-size:34px;font-weight:700;line-height:1.1";
          p.textContent = "We houden de nachten voor je vast en mailen je zo snel mogelijk een bevestiging met de betaalgegevens.";
          p.style.cssText = "margin:0;font-size:17px";
          form.appendChild(h); form.appendChild(p);
          return;
        }
        var b = res.body || {};
        if (b.veld && FIELDS[b.veld]) say(msgForm, FIELDS[b.veld], "fout");
        else say(msgForm, ERRORS[b.error] || ERRORS.niet_beschikbaar, "fout");
        if (b.error === "bezet") { reset(); say(msgDates, ERRORS.bezet, "fout"); }
      })
      .catch(function () { say(msgForm, ERRORS.niet_beschikbaar, "fout"); })
      .then(function () { sending = false; submit.disabled = false; });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (sending) return;
    if (!checkDates()) return;
    if (!priced) showPrice(); else send();
  });

  spots();
})();
