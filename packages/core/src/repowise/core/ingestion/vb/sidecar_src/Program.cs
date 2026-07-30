using System.Text;
using System.Text.Json;
using Repowise.Vb;

// Newline-delimited JSON read loop over stdin/stdout, request/response
// correlated by "id". stdout carries nothing but responses; diagnostics go
// to stderr, which the Python client drains onto structlog at debug.

var jsonOptions = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

using var stdin = Console.OpenStandardInput();
using var stdout = Console.OpenStandardOutput();
using var reader = new StreamReader(stdin, Encoding.UTF8);
using var writer = new StreamWriter(stdout, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false))
{
    AutoFlush = false,
};

string? line;
while ((line = await reader.ReadLineAsync()) is not null)
{
    if (string.IsNullOrWhiteSpace(line))
    {
        continue;
    }

    long id = 0;
    try
    {
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        id = root.TryGetProperty("id", out var idEl) ? idEl.GetInt64() : 0;
        var op = root.TryGetProperty("op", out var opEl) ? opEl.GetString() : null;

        switch (op)
        {
            case "hello":
                var requestedProtocol = root.TryGetProperty("protocol", out var protoEl) ? protoEl.GetInt32() : -1;
                await WriteAsync(writer, new HelloResponse
                {
                    Id = id,
                    Ok = requestedProtocol == Protocol.Version,
                    Protocol = Protocol.Version,
                    Sidecar = Protocol.SidecarVersion,
                    Roslyn = typeof(Microsoft.CodeAnalysis.VisualBasic.VisualBasicSyntaxTree)
                        .Assembly.GetName().Version?.ToString() ?? "unknown",
                    Runtime = Environment.Version.ToString(),
                }, jsonOptions);
                break;

            case "parse":
                var parseReq = root.Deserialize<ParseRequest>(jsonOptions) ?? new ParseRequest();
                var results = new FileParseResult[parseReq.Files.Count];
                Parallel.For(0, parseReq.Files.Count, i =>
                {
                    results[i] = VbExtractor.ParseFile(parseReq.Files[i]);
                });
                await WriteAsync(writer, new ParseResponse
                {
                    Id = id,
                    Ok = true,
                    Results = results.ToList(),
                }, jsonOptions);
                break;

            case "shutdown":
                await WriteAsync(writer, new ShutdownResponse { Id = id, Ok = true }, jsonOptions);
                return 0;

            default:
                await WriteAsync(writer, new ErrorResponse
                {
                    Id = id,
                    Ok = false,
                    Error = $"unknown op '{op}'",
                }, jsonOptions);
                break;
        }
    }
    catch (Exception ex)
    {
        await WriteAsync(writer, new ErrorResponse { Id = id, Ok = false, Error = ex.Message }, jsonOptions);
    }
}

return 0;

static async Task WriteAsync<T>(StreamWriter writer, T payload, JsonSerializerOptions options)
{
    var json = JsonSerializer.Serialize(payload, options);
    await writer.WriteLineAsync(json);
    await writer.FlushAsync();
}
