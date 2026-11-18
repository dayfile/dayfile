// Show the write-source field only when the token type is write.
const kind = document.getElementById("kind");
const field = document.getElementById("source-field");
if (kind && field) {
  const sync = () => { field.hidden = kind.value !== "write"; };
  kind.addEventListener("change", sync);
  sync();
}
