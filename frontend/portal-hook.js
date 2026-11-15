/* Connects the rounds page (index.html) to the portal.
   Add ONE line to index.html, before its main <script>:   <script src="portal-hook.js"></script>
   - Opens the right case:  index.html?case=walter   or   index.html?assignment=ID&student=netid
   - Saves the result for the professor (the back end does that when feedback loads).
   - Adds a "Back to assignments" link. Works with any version of index.html. */
(function () {
  const q = new URLSearchParams(location.search);
  // Served by the back end itself (e.g. on Vultr): talk to the same origin.
  if (!window.WALTER_API && location.port !== '5500' && location.protocol !== 'file:') window.WALTER_API = location.origin;
  const extra = {};
  if (q.get('case')) extra.case_id = q.get('case');
  if (q.get('assignment')) extra.assignment_id = q.get('assignment');
  if (q.get('student')) extra.student_id = q.get('student');
  if (Object.keys(extra).length) {
    const real = window.fetch.bind(window);
    window.fetch = function (url, opts) {
      try {
        const u = String(url);
        if (/\/session\/?(\?|$)/.test(u) && opts && String(opts.method || 'GET').toUpperCase() === 'POST') {
          const body = opts.body ? JSON.parse(opts.body) : {};
          if (extra.assignment_id) body.assignment_id = extra.assignment_id;   // the back end picks the case from the assignment
          if (extra.student_id) body.student_id = extra.student_id;
          if (extra.case_id && !body.case_id) body.case_id = extra.case_id;
          opts = Object.assign({}, opts, { body: JSON.stringify(body), headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}) });
        }
      } catch (e) { /* leave the request alone */ }
      return real(url, opts);
    };
  }
  document.addEventListener('DOMContentLoaded', () => {
    const a = document.createElement('a');
    a.href = 'portal.html' + (q.get('student') ? '?role=student' : '');
    a.textContent = q.get('student') ? '← My assignments' : '← Cases and classes';
    a.style.cssText = 'font:500 14px Inter,system-ui,sans-serif;color:#5c574e;text-decoration:none;white-space:nowrap';
    const bar = document.querySelector('header .bar') || document.querySelector('header');
    const brand = bar && bar.querySelector('.brand');
    if (brand) brand.insertAdjacentElement('afterend', a); else if (bar) bar.prepend(a);
  });
})();
