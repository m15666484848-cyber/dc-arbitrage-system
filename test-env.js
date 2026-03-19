// 测试文件 - 验证 Node.js 环境
console.log('Node.js 环境测试');
console.log('Node 版本:', process.version);
console.log('平台:', process.platform);

// 测试依赖
try {
    const axios = require('axios');
    console.log('✅ Axios 可用');
} catch (err) {
    console.log('❌ Axios 不可用');
}

try {
    const ws = require('ws');
    console.log('✅ WebSocket 可用');
} catch (err) {
    console.log('❌ WebSocket 不可用');
}

console.log('测试完成！');