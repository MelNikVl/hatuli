"""Shared HTML empty states; API error responses keep their JSON contract."""


def not_found_page(templates, request, title, back_url='/map', back_label='К карте'):
    return templates.TemplateResponse('not_found.html', {
        'request': request, 'page_title': title,
        'back_url': back_url, 'back_label': back_label,
    }, status_code=404)
