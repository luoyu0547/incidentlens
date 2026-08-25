import React from 'react';
import { render, Text } from 'ink';

const IncidentLens = () => <Text>IncidentLens CLI v0.1.0</Text>;

const args = process.argv.slice(2);
if (args.includes('--version')) {
  console.log('0.1.0');
  process.exit(0);
}

render(<IncidentLens />);
