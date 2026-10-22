document.querySelector("#checkout-form").addEventListener("submit", (event) => {
  event.preventDefault();
  document.querySelector("#order-status").textContent =
    "Demo order is ready. No payment was collected.";
});
