// Turns a post title into the slug used in its URL.
function slugify(title) {
  return title.toLowerCase().replace(" ", "-");
}

module.exports = { slugify };
