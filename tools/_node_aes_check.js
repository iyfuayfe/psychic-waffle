// 用门户自己的 CryptoJS（从 a41.js 第 1 行原样抽出）加密一组测试串，
// 供 tools/test_aes.py 与纯 Python 实现逐字节比对。
//
// 用法：node _node_aes_check.js <in.json> <out.json>
//   in.json  : JSON 数组，元素为待加密字符串
//   out.json : JSON 数组，元素为对应密文（Base64）
const fs = require('fs');
const path = require('path');

const CryptoJS = require(path.join(__dirname, '_cryptojs.js'));

// 与门户 a41.js 的 util.aes_en 完全一致
function aes_en(str, key = '5c1d5ad4dea0e8dd') {
  return CryptoJS.AES.encrypt(str, CryptoJS.enc.Utf8.parse(key), {
    mode: CryptoJS.mode.ECB,
    padding: CryptoJS.pad.Pkcs7,
  }).toString();
}

const inFile = process.argv[2];
const outFile = process.argv[3];
const items = JSON.parse(fs.readFileSync(inFile, 'utf8'));
const out = items.map((s) => aes_en(s));
fs.writeFileSync(outFile, JSON.stringify(out, null, 2), 'utf8');
console.log('ok:' + out.length);
