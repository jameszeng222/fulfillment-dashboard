const origin = "http://localhost:8770";

export default {
  async fetch(request, env) {
    if (new URL(request.url).origin !== origin) return new Response(null, { status: 403 });
    if (!env.FULFILLMENT_VPC_SERVICE) return new Response(null, { status: 503 });
    return env.FULFILLMENT_VPC_SERVICE.fetch(request);
  },
};
