# 内部接口文档（仅供开发者维护脚本参考）

> **注意**：此文档仅用于维护 `login.py` 脚本时的参考。AI 代理不应直接调用这些接口，所有鉴权操作必须通过 `login.py` 脚本执行。

---

## 接口文档：获取 AppKey

### 接口信息

| 项 | 值 |
|---|---|
| 请求方式 | POST |
| URL | `https://sg-al-cwork-web.mediportal.com.cn/user/appkey/getAppKeyByDingUserId/nologin` |
| 鉴权类型 | `nologin` |
| 需要 token | 否 |

### 请求头

| Header | 值 |
|---|---|
| `Content-Type` | `application/json` |

### 请求体

| 参数名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `requestKey` | string | 是 | 固定值：`MTrBkZsNFFghxH5SmKxWWc93KJqe0` |
| `dingCorpId` | string | 是 | 由 `account_id` 映射得到 |
| `dingUserId` | string | 是 | 使用上下文中的 `send_id` |

### 请求示例

```json
{
  "requestKey": "MTrBkZsNFFghxH5SmKxWWc93KJqe0",
  "dingCorpId": "xxx",
  "dingUserId": "xxx"
}
```

### 响应示例

```json
{
  "resultCode": 1,
  "resultMsg": null,
  "data": {
    "id": 1975,
    "keyName": "dingtalk_user",
    "name": "xxx",
    "appKey": "xxx",
    "expirationDate": "",
    "createTime": ""
  }
}
```

### 字段说明

| 响应字段 | 用途 |
|---|---|
| `data.appKey` | 作为后续 `appKey` 模式接口的 Header 值 |
| `data.name` | 当前用户姓名 |
| `data.expirationDate` | AppKey 过期时间 |
| `data.createTime` | AppKey 创建时间 |

### 说明

- 脚本只需要从响应中提取 `data.appKey`
- `dingCorpId` 的映射逻辑由 `scripts/auth/login.py` 维护
- 若 `send_id` 或 `account_id` 缺失，则不调用本接口

---

## 接口文档：AppKey 换 Token

> 前置说明：如果当前上下文里还没有可用 `appKey`，先参考上方「接口文档：获取 AppKey」获取 `appKey`。

### 接口信息

| 项 | 值 |
|---|---|
| 请求方式 | GET |
| URL | `https://sg-cwork-web.mediportal.com.cn/user/login/appkey` |
| 鉴权类型 | `nologin` |
| 需要 token | 否（本接口用于在已拿到 appKey 后换取 token） |

> 说明：这个接口本身不走业务 header 鉴权，但它要求提供 `appKey` 作为查询参数。

### 请求参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| appCode | string | 是 | 应用编码，固定为 `cms_gpt` |
| appKey | string | 是 | 已获取到的 CWork AppKey |

### 请求示例

```
GET /user/login/appkey?appCode=cms_gpt&appKey=your-app-key
```

### 响应示例

```json
{
  "resultCode": 1,
  "data": {
    "xgToken": "xxxx",
    "userId": "123456",
    "userName": "张三",
    "avatar": "https://...",
    "corpId": "789",
    "personId": "456"
  }
}
```

### 字段映射

| 响应字段 | 用途 |
|---------|------|
| `data.xgToken` | 作为后续请求的 `access-token` Header |
| `data.userId` | 用户 ID |
| `data.userName` | 用户名 |
| `data.corpId` | 企业 ID |
