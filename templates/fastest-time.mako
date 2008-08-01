<%
   import loaddb, query, html
   c = attributes['cursor']

   game_text = \
      html.games_table( query.find_games(c, killertype='winning',
                                         sort_min = 'duration',
                                         limit=3),
                        first = 'duration' )
%>

${game_text}