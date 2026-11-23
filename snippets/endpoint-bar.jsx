export const EndpointBar = ({ method, path }) => {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(`https://api.getbluejay.ai${path}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="bj-endpoint not-prose">
      <span className="bj-method" data-bj-method={method}>{method}</span>
      <code className="bj-endpoint-path">
        {path.split(/(\{[^}]+\})/).map((part, i) =>
          part.startsWith("{") ? <span key={i} className="bj-endpoint-param">{part}</span> : part
        )}
      </code>
      <button type="button" className="bj-endpoint-copy" onClick={copy}>{copied ? "Copied" : "Copy URL"}</button>
    </div>
  );
};
