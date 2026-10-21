"""A simulated Shopify store that publishes WebMCP tools, for browser tests.

Tool names and the checkout object follow Shopify's documentation (UCP checkout shape,
JSON-string arguments, status values, buyer review escalation). Storefront argument
formats are not published by Shopify, so the ones here are plausible stand-ins; the
adapter reads them from each tool's inputSchema exactly as it would on a real store.

Scenario switches (query string on the store URL, persisted in localStorage):
  ?review=1     complete_checkout first asks for buyer review on the page
  ?jump=1       the total goes up after the buyer review
  ?nocard=1     no saved Shop Pay card
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMMON_JS = r"""
<script>
(function(){
  const reg = [];
  document.modelContext = {
    registerTool(t){ reg.push(t); },
    async getTools(){
      return reg.map(t => ({name: t.name, description: t.description,
        inputSchema: JSON.stringify(t.inputSchema || {type:'object', properties:{}}),
        window: window, origin: location.origin}));
    },
    async executeTool(tool, args){
      if (typeof args !== 'string') throw new Error('Failed to parse input arguments');
      const t = reg.find(x => x.name === tool.name);
      const out = await t.execute(JSON.parse(args));
      return out === undefined ? null : JSON.stringify(out);
    },
    addEventListener(){},
  };
  window.__store = {
    get(k, d){ const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); },
    set(k, v){ localStorage.setItem(k, JSON.stringify(v)); },
  };
  const qs = new URLSearchParams(location.search);
  for (const k of ['review', 'jump', 'nocard']) if (qs.has(k)) __store.set('flag_' + k, qs.get(k) === '1');
})();
</script>
"""

CATALOG_JS = r"""
const PRODUCTS = [
  {id: 'gid://shopify/Product/1', handle: 'samba-og', title: 'adidas Samba OG',
   description: 'Leather sneakers shoes', options: ['Color', 'Size'],
   colors: ['White', 'Black'], sizes: ['8', '8.5', '9'], price: '100.00', soldOut: [['Black','8.5']]},
  {id: 'gid://shopify/Product/2', handle: 'crew-socks', title: 'Crew Socks 3-Pack',
   description: 'Cotton socks', colors: ['White'], sizes: ['M'], price: '14.00', soldOut: []},
];
function variantsOf(p){
  const out = []; let n = 0;
  for (const c of p.colors) for (const s of p.sizes) {
    n++;
    out.push({id: p.id.replace('Product', 'ProductVariant') + '-' + n,
      title: c + ' / ' + s, price: {amount: p.price, currencyCode: 'USD'},
      availableForSale: !p.soldOut.some(x => x[0] === c && x[1] === s),
      selectedOptions: [{name: 'Color', value: c}, {name: 'Size', value: s}]});
  }
  return out;
}
function findVariant(id){
  for (const p of PRODUCTS) for (const v of variantsOf(p)) if (v.id === id) return [p, v];
  return [null, null];
}
"""

STOREFRONT = (
    "<!doctype html><html><head><title>Sneaker Co</title>"
    + COMMON_JS
    + "<script>"
    + CATALOG_JS
    + r"""
const mc = document.modelContext;
mc.registerTool({name: 'search_catalog', description: 'Search products',
  inputSchema: {type: 'object', properties: {query: {type: 'string'}, limit: {type: 'integer'}},
                required: ['query']},
  execute: async ({query}) => {
    const q = query.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = PRODUCTS.filter(p => q.some(w => (p.title + ' ' + p.description).toLowerCase().includes(w.replace(/s$/, ''))));
    return {products: hits.map(p => ({id: p.id, handle: p.handle, title: p.title,
      price_range: {min: p.price}, url: location.origin + '/products/' + p.handle}))};
  }});
mc.registerTool({name: 'get_product', description: 'Product details',
  inputSchema: {type: 'object', properties: {product_id: {type: 'string'}}},
  execute: async ({product_id}) => {
    const p = PRODUCTS.find(x => x.id === product_id);
    if (!p) return {ucp: {status: 'error'}, messages: [{type: 'error', code: 'invalid_request', content: 'no product'}]};
    return {product: {id: p.id, title: p.title, variants: {nodes: variantsOf(p)}}};
  }});
mc.registerTool({name: 'get_cart', description: 'Cart',
  execute: async () => ({lines: __store.get('cart', [])})});
mc.registerTool({name: 'update_cart', description: 'Change the cart',
  inputSchema: {type: 'object', properties: {
    add_items: {type: 'array', items: {type: 'object', properties: {
      variant_id: {type: 'string'}, quantity: {type: 'integer'}}}},
    remove_line_ids: {type: 'array', items: {type: 'string'}}}},
  execute: async ({add_items}) => {
    const cart = __store.get('cart', []);
    for (const it of add_items || []) {
      const [p, v] = findVariant(it.variant_id);
      if (!v || !v.availableForSale) return {ucp: {status: 'error'}, messages: [{type: 'error', code: 'rejected', content: 'unavailable'}]};
      cart.push({id: 'line-' + (cart.length + 1), variant_id: v.id, title: p.title, price: Math.round(parseFloat(v.price.amount) * 100), quantity: it.quantity});
    }
    __store.set('cart', cart);
    return {lines: cart};
  }});
mc.registerTool({name: 'cancel_cart', description: 'Empty the cart',
  execute: async () => { __store.set('cart', []); __store.set('co', null); return {lines: []}; }});
mc.registerTool({name: 'proceed_to_checkout', description: 'Go to checkout',
  execute: async () => {
    if (!__store.get('cart', []).length) return {ucp: {status: 'error'}, messages: [{type: 'error', code: 'invalid_request', content: 'empty cart'}]};
    setTimeout(() => { location.href = '/checkouts/cn_123'; }, 10);
    return {navigation_started: true};
  }});
</script></head><body><h1>Sneaker Co</h1></body></html>
"""
)

CHECKOUT = (
    "<!doctype html><html><head><title>Checkout</title>"
    + COMMON_JS
    + r"""
<script>
const mc = document.modelContext;
function state(){
  let co = __store.get('co', null);
  if (!co) {
    co = {id: 'cn_123', buyer: {}, destination: null, option: null, instrument: null,
          review: 'none', completed: false, order: null};
    __store.set('co', co);
  }
  return co;
}
function render(){
  const co = state(); const cart = __store.get('cart', []);
  const sub = cart.reduce((a, l) => a + l.price * l.quantity, 0);
  const opts = [{id: 'std', title: 'Standard', total: 499}, {id: 'exp', title: 'Express', total: 1299}];
  const ship = co.option ? opts.find(o => o.id === co.option).total : 0;
  const bump = (__store.get('flag_jump', false) && co.review === 'approved') ? 1000 : 0;
  const instruments = __store.get('flag_nocard', false) ? [] :
    [{id: 'pi_saved_1', type: 'card', selected: co.instrument === 'pi_saved_1',
      display: {brand: 'visa', last_digits: '4242'}}];
  let status = 'incomplete';
  if (co.completed) status = 'completed';
  else if (co.review === 'pending') status = 'requires_escalation';
  else if (co.buyer.email && co.destination && co.option && co.instrument) status = 'ready_for_complete';
  const out = {ucp: {version: '2026-04-08', status: 'success'}, id: co.id, status,
    currency: 'USD', buyer: co.buyer,
    line_items: cart.map(l => ({id: l.id, item: {id: l.variant_id, title: l.title, price: l.price}, quantity: l.quantity})),
    fulfillment: {methods: [{type: 'shipping',
      destinations: co.destination ? [Object.assign({id: 'dest_1'}, co.destination)] : [],
      groups: co.destination ? [{id: 'g1', selected_option_id: co.option, options: opts}] : []}]},
    payment: {instruments},
    totals: [{type: 'subtotal', amount: sub + bump}, {type: 'fulfillment', amount: ship},
             {type: 'tax', amount: 0}, {type: 'total', amount: sub + bump + ship}],
    messages: co.review === 'pending' ? [{type: 'warning', code: 'buyer_review_required', severity: 'requires_buyer_review'}] : []};
  if (co.order) out.order = co.order;
  return out;
}
mc.registerTool({name: 'get_checkout', description: 'Read checkout', execute: async () => render()});
mc.registerTool({name: 'update_checkout', description: 'Update checkout (PUT)',
  execute: async ({checkout}) => {
    const co = state();
    if (checkout.buyer) co.buyer = checkout.buyer;
    const m = ((checkout.fulfillment || {}).methods || [])[0];
    if (m) {
      if ((m.destinations || [])[0]) co.destination = m.destinations[0];
      for (const g of m.groups || []) if (g.selected_option_id) co.option = g.selected_option_id;
    }
    const ins = ((checkout.payment || {}).instruments || [])[0];
    if (ins && ins.id) co.instrument = ins.id;
    __store.set('co', co);
    return render();
  }});
mc.registerTool({name: 'complete_checkout', description: 'Place the order',
  execute: async () => {
    const co = state(); const cur = render();
    if (cur.status === 'requires_escalation') return cur;
    if (cur.status !== 'ready_for_complete') return {ucp: {status: 'error'}, messages: [{type: 'error', code: 'invalid_request', content: 'not ready'}]};
    if (__store.get('flag_review', false) && co.review === 'none') {
      co.review = 'pending'; __store.set('co', co);
      // The buyer reviews the order on the page and approves it a moment later.
      setTimeout(() => { const c = state(); c.review = 'approved'; __store.set('co', c); }, 1200);
      const r = render(); r.continue_url = location.href; return r;
    }
    co.completed = true;
    co.order = {id: 'gid://shopify/Order/9001', label: '#1001', permalink_url: location.origin + '/orders/9001'};
    __store.set('co', co); __store.set('orders', (__store.get('orders', 0)) + 1);
    return render();
  }});
mc.registerTool({name: 'navigate_to_storefront', description: 'Back to store',
  execute: async () => { setTimeout(() => { location.href = '/'; }, 10);
    return {status: 'navigation_started', destination_url: location.origin + '/'}; }});
// The buyer approving Shopify's review step on the page:
window.__buyerApproves = () => { const co = state(); co.review = 'approved'; __store.set('co', co); };
window.__orders = () => __store.get('orders', 0);
</script></head><body><h1>Checkout</h1></body></html>
"""
)


class FakeShopify:
    def __init__(self) -> None:
        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = CHECKOUT if self.path.startswith("/checkouts/") else STOREFRONT
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
