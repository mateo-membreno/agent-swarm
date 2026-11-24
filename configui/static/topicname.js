// Turns whatever a person types into a valid topic name: "News stories!" -> "news_stories.v1".
// A name that is already valid (news_stories.v2, email.triaged.v1) is left alone. The .v<N> suffix is the topic's
// version: a breaking schema change makes a new topic (.v2) instead of rewriting what is already on the bus.
(function () {
  const VALID = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\.v[1-9][0-9]*$/;
  window.topicName = function topicName(text) {
    const t = String(text || "").trim();
    if (VALID.test(t)) return t;
    let slug = t.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (!slug) return "";
    if (/^[0-9]/.test(slug)) slug = `t_${slug}`;
    return `${slug.slice(0, 48)}.v1`;
  };
})();
