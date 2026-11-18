// Copy the account details to the clipboard; the button stays hidden where that is not possible.
const copy = document.getElementById("copy-account");
const text = document.getElementById("account-json");
if (copy && text && navigator.clipboard) {
  copy.hidden = false;
  copy.addEventListener("click", () => {
    navigator.clipboard.writeText(text.textContent).then(() => {
      copy.textContent = "Copied";
      setTimeout(() => { copy.textContent = "Copy"; }, 2000);
    });
  });
}
