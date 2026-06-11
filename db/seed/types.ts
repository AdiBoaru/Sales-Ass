export interface DemoBrand {
  slug: string;
  name: string;
  [key: string]: unknown;
}

export interface DemoCategory {
  slug: string;
  name: string;
  parentSlug?: string;
}

export interface DemoImage {
  url: string;
  alt?: string;
  position: number;
}

export interface DemoVariant {
  label: string;
  sku: string;
  price: number;
  salePrice?: number;
  stock: number;
  colorHex?: string;
  attributes: Record<string, unknown>;
}

export interface DemoSection {
  kind: string;
  title?: string;
  body: string;
}

export interface DemoIngredient {
  name: string;
  position: number;
  isKey: boolean;
}

export interface DemoReview {
  author?: string;
  rating: number;
  body?: string;
  createdAt?: string;
}

export interface DemoProduct {
  name: string;
  slug: string;
  brandSlug: string;
  primaryCategorySlug?: string;
  categorySlugs: string[];
  shortDescription?: string;
  description?: string;
  badges: string[];
  images: DemoImage[];
  variants: DemoVariant[];
  sections: DemoSection[];
  ingredients: DemoIngredient[];
  reviews: DemoReview[];
  attributes: Record<string, unknown>;
  currency: string;
  price: number;
  salePrice?: number;
  rating?: number;
  reviewCount?: number;
  status: string;
  sourceFingerprint?: string;
}

export interface DemoCatalog {
  brands: DemoBrand[];
  categories: DemoCategory[];
  products: DemoProduct[];
}

export interface SourceProduct {
  sourceSite: string;
  sourceUrl: string;
  scrapedAt: string;
  [key: string]: unknown;
}
