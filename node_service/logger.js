function getTimestamp() {
  const now = new Date();
  const dd = String(now.getDate()).padStart(2, '0');
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  const yy = String(now.getFullYear()).slice(-2);
  const hh = String(now.getHours()).padStart(2, '0');
  const min = String(now.getMinutes()).padStart(2, '0');
  return `${dd}/${mm}/${yy} ${hh}:${min}`;
}

function log_info(message) {
  console.log(`${getTimestamp()} ${message}`);
}

function log_error(message) {
  console.error(`${getTimestamp()} ${message}`);
}

module.exports = {
  log_info: log_info,
  log_error: log_error,
  getTimestamp: getTimestamp
};
