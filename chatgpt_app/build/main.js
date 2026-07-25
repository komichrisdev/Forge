import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer } from "./server.js";
export const HOST = "127.0.0.1";
export const PORT = 8790;
export function startHttpServer(port = PORT) {
    const app = createMcpExpressApp({ host: HOST });
    app.all("/mcp", async (req, res) => {
        const server = createServer();
        const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
        res.on("close", () => { void transport.close(); void server.close(); });
        try {
            await server.connect(transport);
            await transport.handleRequest(req, res, req.body);
        }
        catch {
            if (!res.headersSent)
                res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "Internal server error" }, id: null });
        }
    });
    app.use((_req, res) => res.status(404).send("Not found"));
    return app.listen(port, HOST, () => console.log(`Swarm Control MCP listening on http://${HOST}:${port}/mcp`));
}
if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
    const http = startHttpServer();
    const stop = () => http.close(() => process.exit(0));
    process.on("SIGINT", stop);
    process.on("SIGTERM", stop);
}
//# sourceMappingURL=main.js.map