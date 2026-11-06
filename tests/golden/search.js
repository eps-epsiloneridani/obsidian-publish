/* Dependency-free client-side search for the static site.
 *
 * Loads search-index.json (built at build time by build.py) once, then filters
 * it live as the user types. No libraries, no CDN, no network beyond the one
 * local fetch of the index.
 *
 * The page embeds `window.SEARCH_ROOT` (the relative path from the current
 * page's directory back to the site root), so links and the index fetch work
 * regardless of how deep in the tree the current page sits.
 */
(function () {
  "use strict";

  var root = window.SEARCH_ROOT || "";
  if (root.length && root.charAt(root.length - 1) !== "/") {
    root += "/";
  }

  var index = [];

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function tokenize(q) {
    return q.toLowerCase().split(/\s+/).filter(function (t) {
      return t.length > 0;
    });
  }

  function score(item, tokens) {
    var title = (item.title || "").toLowerCase();
    var text = (item.text || "").toLowerCase();
    var s = 0;
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (title.indexOf(t) !== -1) {
        s += 3; // title match weighs more than body match
      }
      if (text.indexOf(t) !== -1) {
        s += 1;
      }
    }
    return s;
  }

  function renderResults(tokens) {
    var box = document.getElementById("search-results");
    if (!box) {
      return;
    }
    if (!tokens.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    var scored = [];
    for (var i = 0; i < index.length; i++) {
      var s = score(index[i], tokens);
      if (s > 0) {
        scored.push({ item: index[i], score: s });
      }
    }
    scored.sort(function (a, b) {
      return b.score - a.score || a.item.title.localeCompare(b.item.title);
    });
    scored = scored.slice(0, 10);

    if (!scored.length) {
      box.hidden = false;
      box.innerHTML = '<li class="search-empty">No matches</li>';
      return;
    }
    var html = "";
    for (var j = 0; j < scored.length; j++) {
      var it = scored[j].item;
      html +=
        '<li><a href="' + root + it.path + '">' + escapeHtml(it.title) + "</a></li>";
    }
    box.innerHTML = html;
    box.hidden = false;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var input = document.getElementById("search-input");
    if (!input) {
      return;
    }

    fetch(root + "search-index.json")
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        index = data;
      })
      .catch(function () {
        index = [];
      });

    input.addEventListener("input", function () {
      renderResults(tokenize(input.value));
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        input.value = "";
        renderResults([]);
        input.blur();
      } else if (e.key === "Enter") {
        var first = document.querySelector("#search-results a");
        if (first) {
          first.click();
        }
      }
    });

    document.addEventListener("click", function (e) {
      var box = document.getElementById("search-results");
      if (box && !box.contains(e.target) && e.target !== input) {
        box.hidden = true;
      }
    });
  });
})();
