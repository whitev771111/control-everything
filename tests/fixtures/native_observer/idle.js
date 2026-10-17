// A synthetic public CLI entry: no provider, network, model, or account access.
process.stdin.setEncoding('utf8');
process.stdin.on('data', text => {
  if (text.includes('fixture-stop')) process.exit(0);
  process.stdout.write(text);
});
process.stdin.resume();
setInterval(() => {}, 1000);
