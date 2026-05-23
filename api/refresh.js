// api/refresh.js — GitHub Actions workflow_dispatch 트리거
// Vercel 환경변수 GITHUB_PAT 필요 (workflow 권한 포함된 Personal Access Token)

export default async function handler(req, res) {
  // CORS 허용
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');

  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const token = process.env.GITHUB_PAT;
  if (!token) {
    return res.status(500).json({ error: 'GITHUB_PAT 환경변수가 설정되지 않았습니다.' });
  }

  try {
    const response = await fetch(
      'https://api.github.com/repos/yonhongso/portfolio-agent/actions/workflows/daily.yml/dispatches',
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Accept': 'application/vnd.github.v3+json',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ ref: 'main' }),
      }
    );

    if (response.status === 204) {
      return res.status(200).json({
        success: true,
        message: '업데이트가 시작되었습니다. 약 2~3분 후 새로고침 해주세요.'
      });
    } else {
      const errorText = await response.text();
      return res.status(response.status).json({ error: errorText });
    }
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
}
