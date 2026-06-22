/**
 * 品牌配置 - 根据 VITE_BOB_RELEASE 环境变量返回不同的产品信息
 */

export const BOB_RELEASE = import.meta.env.VITE_BOB_RELEASE === true;


// 品牌配置
const BRAND = {
  default: {
    name: "Table Talk",
    description: "AI 驱动的 Excel 智能处理",
    footer: "让数据处理更简单。",
  },
  bob: {
    name: "智算数据处理系统",
    description: "AI 驱动的 Excel 智能处理",
    footer: "让数据处理更简单。",
  },
};

function getBranding() {
  return BOB_RELEASE ? BRAND.bob : BRAND.default;
}

export function getProductName(): string {
  return getBranding().name;
}

export function getProductDescription(): string {
  return getBranding().description;
}

export function getProductFooter(): string {
  return getBranding().footer;
}

