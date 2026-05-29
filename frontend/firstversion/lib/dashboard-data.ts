// --- Types ---

export type Campaign = {
  name: string
  platform: string
  spend: number
  leads: number
  cpl: number
  conversions: number
  revenue: number
  roas: number
  status: string
}

export type WeeklyPoint = { week: string; leads: number; qualified: number }
export type SourcePoint = { source: string; leads: number; percentage: number }
export type DailyPoint = { date: string; spend: number; leads: number; revenue: number }

export type LeadData = {
  total: number
  thisMonth: number
  qualified: number
  conversionRate: number
  weeklyTrend: WeeklyPoint[]
  sourceBreakdown: SourcePoint[]
}

export type CampaignData = {
  totalSpend: number
  totalRevenue: number
  roas: number
  cpl: number
  campaigns: Campaign[]
  dailyPerformance: DailyPoint[]
}

export type Lead = {
  id: string
  name: string
  company: string
  email: string
  source: string
  status: string
  date: string
}

export type DashboardData = {
  leadData: LeadData
  campaignData: CampaignData
  recentLeads: Lead[]
}

// --- Static data (not fetched from Supabase) ---

export const insights = [
  {
    type: "positive",
    title: "Meta Ads outperforming",
    description: "Product Launch campaign ROAS is 15% above target at 3.73x",
  },
  {
    type: "warning",
    title: "LinkedIn CPL elevated",
    description: "B2B campaign CPL at $93.37 — consider audience refinement",
  },
  {
    type: "positive",
    title: "Lead quality improving",
    description: "Qualified lead rate increased 8% vs. last month",
  },
  {
    type: "neutral",
    title: "Weekend performance",
    description: "Saturday/Sunday leads down 45% — typical pattern confirmed",
  },
]

// Platform-specific campaign data (used on campaigns page — mock until that page is connected)
export const platformData = {
  meta: {
    name: "Meta Ads",
    color: "hsl(var(--chart-1))",
    totalSpend: 24800,
    totalRevenue: 89200,
    roas: 3.60,
    impressions: 1280000,
    clicks: 22400,
    ctr: 1.75,
    leads: 412,
    cpl: 60.19,
    conversions: 52,
    campaigns: [
      { id: "meta-1", name: "Product Launch - Lookalike", objective: "Conversions", status: "active", spend: 12400, budget: 15000, impressions: 680000, clicks: 12200, ctr: 1.79, leads: 198, cpl: 62.63, conversions: 28, revenue: 48600, roas: 3.92, startDate: "2024-01-15", audience: "Lookalike 1%" },
      { id: "meta-2", name: "Retargeting - Website Visitors", objective: "Conversions", status: "active", spend: 6200, budget: 8000, impressions: 320000, clicks: 5800, ctr: 1.81, leads: 124, cpl: 50.00, conversions: 18, revenue: 28400, roas: 4.58, startDate: "2024-01-20", audience: "Website Visitors 30d" },
      { id: "meta-3", name: "Brand Awareness - Interest", objective: "Awareness", status: "active", spend: 4200, budget: 5000, impressions: 180000, clicks: 2800, ctr: 1.56, leads: 56, cpl: 75.00, conversions: 4, revenue: 8200, roas: 1.95, startDate: "2024-02-01", audience: "Interest Targeting" },
      { id: "meta-4", name: "Lead Gen - Carousel", objective: "Lead Generation", status: "paused", spend: 2000, budget: 4000, impressions: 100000, clicks: 1600, ctr: 1.60, leads: 34, cpl: 58.82, conversions: 2, revenue: 4000, roas: 2.00, startDate: "2024-01-25", audience: "Custom Audience" },
    ],
    weeklyPerformance: [
      { week: "W1", spend: 5200, leads: 89, revenue: 19200 },
      { week: "W2", spend: 6400, leads: 108, revenue: 23400 },
      { week: "W3", spend: 6100, leads: 102, revenue: 22100 },
      { week: "W4", spend: 7100, leads: 113, revenue: 24500 },
    ],
  },
  google: {
    name: "Google Ads",
    color: "hsl(var(--chart-2))",
    totalSpend: 18400,
    totalRevenue: 58200,
    roas: 3.16,
    impressions: 890000,
    clicks: 12400,
    ctr: 1.39,
    leads: 298,
    cpl: 61.74,
    conversions: 38,
    campaigns: [
      { id: "google-1", name: "Brand Search", objective: "Search", status: "active", spend: 4200, budget: 5000, impressions: 120000, clicks: 4800, ctr: 4.00, leads: 86, cpl: 48.84, conversions: 14, revenue: 18200, roas: 4.33, startDate: "2024-01-01", audience: "Brand Keywords" },
      { id: "google-2", name: "Non-Brand Search", objective: "Search", status: "active", spend: 8600, budget: 10000, impressions: 340000, clicks: 4200, ctr: 1.24, leads: 112, cpl: 76.79, conversions: 12, revenue: 21400, roas: 2.49, startDate: "2024-01-01", audience: "Generic Keywords" },
      { id: "google-3", name: "Display Remarketing", objective: "Display", status: "active", spend: 3400, budget: 4000, impressions: 320000, clicks: 2200, ctr: 0.69, leads: 62, cpl: 54.84, conversions: 8, revenue: 12400, roas: 3.65, startDate: "2024-01-10", audience: "Remarketing List" },
      { id: "google-4", name: "Performance Max", objective: "Performance Max", status: "active", spend: 2200, budget: 3000, impressions: 110000, clicks: 1200, ctr: 1.09, leads: 38, cpl: 57.89, conversions: 4, revenue: 6200, roas: 2.82, startDate: "2024-02-05", audience: "Auto Optimized" },
    ],
    weeklyPerformance: [
      { week: "W1", spend: 4100, leads: 68, revenue: 13200 },
      { week: "W2", spend: 4600, leads: 74, revenue: 14800 },
      { week: "W3", spend: 4500, leads: 72, revenue: 14400 },
      { week: "W4", spend: 5200, leads: 84, revenue: 15800 },
    ],
  },
  linkedin: {
    name: "LinkedIn Ads",
    color: "hsl(var(--chart-5))",
    totalSpend: 12800,
    totalRevenue: 24600,
    roas: 1.92,
    impressions: 280000,
    clicks: 3700,
    ctr: 1.32,
    leads: 142,
    cpl: 90.14,
    conversions: 18,
    campaigns: [
      { id: "linkedin-1", name: "B2B Decision Makers", objective: "Lead Generation", status: "active", spend: 6400, budget: 8000, impressions: 140000, clicks: 1900, ctr: 1.36, leads: 68, cpl: 94.12, conversions: 8, revenue: 12400, roas: 1.94, startDate: "2024-01-15", audience: "Director+ Titles" },
      { id: "linkedin-2", name: "Thought Leadership", objective: "Engagement", status: "active", spend: 3200, budget: 4000, impressions: 80000, clicks: 1100, ctr: 1.38, leads: 42, cpl: 76.19, conversions: 6, revenue: 7200, roas: 2.25, startDate: "2024-01-20", audience: "Industry Followers" },
      { id: "linkedin-3", name: "ABM - Enterprise", objective: "Account-Based", status: "paused", spend: 3200, budget: 5000, impressions: 60000, clicks: 700, ctr: 1.17, leads: 32, cpl: 100.00, conversions: 4, revenue: 5000, roas: 1.56, startDate: "2024-02-01", audience: "Target Account List" },
    ],
    weeklyPerformance: [
      { week: "W1", spend: 2800, leads: 32, revenue: 5400 },
      { week: "W2", spend: 3200, leads: 36, revenue: 6200 },
      { week: "W3", spend: 3400, leads: 38, revenue: 6600 },
      { week: "W4", spend: 3400, leads: 36, revenue: 6400 },
    ],
  },
}
